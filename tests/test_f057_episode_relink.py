"""F057 episode re-linker tests.

Mock-based unit tests for the phase's control flow + an integration
test that exercises the production SQL against a real Postgres.

The phase backfills F022 episode-graph edges that the live linker
missed (most commonly: stuck-open sessions that never received
``episode_ended``). It queries active orphan episodes older than
``episode_relink_min_age_hours`` that have at least one linkable
anchor (active fact via ``source_episode_id`` or row in
``heart.episode_decisions``), then calls
``graph_linker.link_episode_deterministic`` on each.

Skip rules for the integration test:
  - ``@pytest.mark.integration``  → only with ``--integration`` flag
  - ``@pytest.mark.postgres_only`` → only with ``NOUS_TEST_DB=postgres``

Run integration manually:
    NOUS_TEST_DB=postgres uv run pytest \\
      tests/test_f057_episode_relink.py -m integration --integration -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.events import EventBus


def _make_handler(
    *,
    episode_relink_enabled: bool = True,
    episode_relink_min_age_hours: int = 24,
    episode_relink_max_per_cycle: int = 30,
    candidate_eps: list | None = None,
    fact_anchors_per_ep: dict | None = None,
    decision_anchors_per_ep: dict | None = None,
    raise_on_link: bool = False,
    raise_on_session: bool = False,
):
    """Construct a SleepHandler with mocked Heart.db.session() returning
    a CM whose execute() responds to F055's specific SQL pattern.

    We dispatch on SQL string content:
      - ``WHERE e.agent_id`` (the candidate query) → returns
        ``[(ep_id,) ...]`` from candidate_eps
      - ``f.source_episode_id`` (fact anchors) → returns the configured
        fact_ids for that episode
      - ``ed.episode_id`` (decision anchors) → returns configured
        decision_ids
    """
    from nous.handlers.sleep_handler import SleepHandler

    candidate_eps = candidate_eps or []
    fact_anchors_per_ep = fact_anchors_per_ep or {}
    decision_anchors_per_ep = decision_anchors_per_ep or {}

    brain = AsyncMock()
    heart = AsyncMock()
    settings = Settings(_env_file=None)
    object.__setattr__(settings, "episode_relink_enabled", episode_relink_enabled)
    object.__setattr__(
        settings, "episode_relink_min_age_hours", episode_relink_min_age_hours
    )
    object.__setattr__(
        settings, "episode_relink_max_per_cycle", episode_relink_max_per_cycle
    )
    bus = MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    llm_client = AsyncMock()

    # Build the session mock with execute dispatch
    mock_session = MagicMock()
    mock_session.commit = AsyncMock()

    def make_result(rows):
        result = MagicMock()
        # Phase reads via .all() then list-comprehends r[0]
        result.all = MagicMock(return_value=rows)
        return result

    async def execute_dispatch(sql, params=None):
        if raise_on_session:
            raise RuntimeError("session boom")
        sql_str = str(sql)
        # Dispatch on FROM clause to avoid the candidate query's nested
        # EXISTS clauses leaking into anchor-query routing.
        if "FROM heart.episodes" in sql_str:
            return make_result([(eid,) for eid in candidate_eps])
        if "FROM heart.facts" in sql_str:
            ep_id = (params or {}).get("eid")
            ids = fact_anchors_per_ep.get(ep_id, [])
            return make_result([(fid,) for fid in ids])
        if "FROM heart.episode_decisions" in sql_str:
            ep_id = (params or {}).get("eid")
            ids = decision_anchors_per_ep.get(ep_id, [])
            return make_result([(did,) for did in ids])
        return make_result([])

    mock_session.execute = AsyncMock(side_effect=execute_dispatch)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=False)
    heart.db = MagicMock()
    heart.db.session = MagicMock(return_value=cm)
    heart._embeddings = None  # not used by deterministic linker
    heart.agent_id = settings.agent_id

    # Inject a stub graph_linker so we don't need the real GraphLinker
    handler = SleepHandler(brain, heart, settings, bus, llm_client)

    async def fake_link(episode_id, decision_ids, fact_ids, session):
        if raise_on_link:
            raise RuntimeError("link boom")
        # Return one stub edge per provided id (mirrors real signature)
        return [object()] * (len(decision_ids) + len(fact_ids))

    handler._graph_linker = MagicMock()
    handler._graph_linker.link_episode_deterministic = AsyncMock(side_effect=fake_link)
    return handler, mock_session


class TestF057EpisodeRelink:
    @pytest.mark.asyncio
    async def test_default_settings_have_flag_on(self):
        s = Settings(_env_file=None)
        assert s.episode_relink_enabled is True
        assert s.episode_relink_min_age_hours == 24
        assert s.episode_relink_max_per_cycle == 30

    @pytest.mark.asyncio
    async def test_flag_off_is_noop(self):
        handler, mock_session = _make_handler(episode_relink_enabled=False)
        sleep_stats: dict = {}
        result = await handler._phase_relink_open_episodes(sleep_stats)
        assert result is True
        mock_session.execute.assert_not_called()
        assert sleep_stats == {}

    @pytest.mark.asyncio
    async def test_max_per_cycle_zero_is_noop(self):
        handler, mock_session = _make_handler(episode_relink_max_per_cycle=0)
        sleep_stats: dict = {}
        result = await handler._phase_relink_open_episodes(sleep_stats)
        assert result is True
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_candidates_returns_success_zero(self):
        handler, mock_session = _make_handler(candidate_eps=[])
        sleep_stats: dict = {}
        result = await handler._phase_relink_open_episodes(sleep_stats)
        assert result is True
        assert sleep_stats == {
            "episodes_relinked": 0,
            "episode_relink_edges": 0,
        }

    @pytest.mark.asyncio
    async def test_relink_records_count_and_edges(self):
        ep1, ep2 = uuid4(), uuid4()
        f1a, f1b, d2 = uuid4(), uuid4(), uuid4()
        handler, mock_session = _make_handler(
            candidate_eps=[ep1, ep2],
            fact_anchors_per_ep={ep1: [f1a, f1b]},
            decision_anchors_per_ep={ep2: [d2]},
        )
        sleep_stats: dict = {}
        result = await handler._phase_relink_open_episodes(sleep_stats)
        assert result is True
        assert sleep_stats["episodes_relinked"] == 2
        # ep1 → 2 fact edges, ep2 → 1 decision edge → 3 total
        assert sleep_stats["episode_relink_edges"] == 3
        # commit called once at end
        mock_session.commit.assert_awaited_once()
        # link_episode_deterministic called twice (one per ep with anchors)
        assert handler._graph_linker.link_episode_deterministic.await_count == 2

    @pytest.mark.asyncio
    async def test_episode_with_no_anchors_skipped(self):
        """If a candidate has neither facts nor decisions, the loop
        skips it (no link call, no edge counted)."""
        ep1, ep2 = uuid4(), uuid4()
        handler, mock_session = _make_handler(
            candidate_eps=[ep1, ep2],
            fact_anchors_per_ep={ep1: [uuid4()]},  # ep1 has 1 fact
            decision_anchors_per_ep={},  # ep2 has neither
        )
        sleep_stats: dict = {}
        await handler._phase_relink_open_episodes(sleep_stats)
        assert sleep_stats["episodes_relinked"] == 1  # only ep1
        assert handler._graph_linker.link_episode_deterministic.await_count == 1

    @pytest.mark.asyncio
    async def test_per_episode_link_failure_records_error(self):
        """When the linker raises for one episode, we log + count the
        error but continue with the next candidate."""
        ep1, ep2 = uuid4(), uuid4()
        handler, mock_session = _make_handler(
            candidate_eps=[ep1, ep2],
            fact_anchors_per_ep={ep1: [uuid4()], ep2: [uuid4()]},
            raise_on_link=True,
        )
        sleep_stats: dict = {}
        result = await handler._phase_relink_open_episodes(sleep_stats)
        # Phase still succeeds — per-episode errors don't abort
        assert result is True
        assert sleep_stats["episodes_relinked"] == 0
        assert sleep_stats["episode_relink_errors"] == 2

    @pytest.mark.asyncio
    async def test_session_exception_returns_false_with_error_type(self):
        """A session-wide exception (not a per-episode one) marks the
        phase as failed and surfaces the exception type for ops."""
        handler, mock_session = _make_handler(raise_on_session=True)
        sleep_stats: dict = {}
        result = await handler._phase_relink_open_episodes(sleep_stats)
        assert result is False
        assert sleep_stats.get("episode_relink_phase_error") == "RuntimeError"
        # commit MUST NOT happen on error
        mock_session.commit.assert_not_called()


# ===========================================================================
# Integration test — real Postgres, end-to-end behavior
# ===========================================================================


@pytest.mark.integration
@pytest.mark.postgres_only
class TestF057Integration:
    """End-to-end F057 against real Postgres.

    Inserts: 1 active orphan episode old enough to qualify, 2 active
    facts referencing it via source_episode_id, 1 inactive fact
    (should NOT be picked up). Runs the phase. Asserts:
      - sleep_stats[episodes_relinked] == 1
      - sleep_stats[episode_relink_edges] == 2 (both active facts)
      - 2 new episode→fact edges in brain.graph_edges
      - the inactive fact didn't produce an edge
    """

    @pytest.mark.asyncio
    async def test_phase_relinks_orphan_with_fact_anchors(
        self, db, mock_embeddings,
    ):
        from datetime import UTC, datetime, timedelta
        from sqlalchemy import text as sql_text

        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler
        from nous.heart import Heart

        agent_id = f"f057-it-{uuid4().hex[:8]}"
        ep_id = uuid4()
        fact_active_a = uuid4()
        fact_active_b = uuid4()
        fact_inactive = uuid4()
        old = datetime.now(UTC) - timedelta(days=2)

        try:
            async with db.session() as fs:
                await fs.execute(sql_text(
                    "INSERT INTO nous_system.agents (id, name) "
                    "VALUES (:aid, :name) ON CONFLICT (id) DO NOTHING"
                ), {"aid": agent_id, "name": "F057 IT agent"})
                # Active orphan episode (no incident edges; old enough)
                await fs.execute(sql_text(
                    "INSERT INTO heart.episodes "
                    "(id, agent_id, summary, started_at, active, tags) "
                    "VALUES (:id, :aid, :s, :t, true, '{}')"
                ), {
                    "id": ep_id, "aid": agent_id,
                    "s": "F057 IT episode summary", "t": old,
                })
                # 2 active facts + 1 inactive fact, all source_episode_id=ep_id
                for fid, active in [
                    (fact_active_a, True), (fact_active_b, True),
                    (fact_inactive, False),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.facts "
                        "(id, agent_id, content, source_episode_id, "
                        " active, created_at) "
                        "VALUES (:id, :aid, :c, :eid, :a, :t)"
                    ), {
                        "id": fid, "aid": agent_id,
                        "c": f"f057 IT fact {fid}", "eid": ep_id,
                        "a": active, "t": old,
                    })
                await fs.commit()

            settings = Settings(_env_file=None)
            object.__setattr__(settings, "agent_id", agent_id)
            object.__setattr__(settings, "episode_relink_enabled", True)
            object.__setattr__(settings, "episode_relink_min_age_hours", 1)
            object.__setattr__(settings, "episode_relink_max_per_cycle", 10)

            heart = Heart(db, settings, embedding_provider=mock_embeddings)
            brain = AsyncMock()
            bus = MagicMock(spec=EventBus)
            bus.on = MagicMock()
            bus.emit = AsyncMock()
            llm_client = AsyncMock()
            handler = SleepHandler(brain, heart, settings, bus, llm_client)

            sleep_stats: dict = {}
            result = await handler._phase_relink_open_episodes(sleep_stats)

            assert result is True
            assert sleep_stats.get("episodes_relinked") == 1
            # 2 active facts → 2 episode→fact edges; inactive fact excluded
            assert sleep_stats.get("episode_relink_edges") == 2

            # Verify edges exist in DB. The linker creates fact→episode
            # edges (relation='extracted_from'), so the episode is the
            # TARGET endpoint and facts are sources.
            async with db.session() as vs:
                rows = await vs.execute(sql_text(
                    "SELECT source_id FROM brain.graph_edges "
                    "WHERE agent_id=:aid AND target_id=:eid "
                    "AND target_type='episode' AND source_type='fact'"
                ), {"aid": agent_id, "eid": ep_id})
                edge_targets = {r.source_id for r in rows.all()}
            assert edge_targets == {fact_active_a, fact_active_b}, (
                f"expected edges to both active facts, got {edge_targets}"
            )
            await heart.close()
        finally:
            async with db.session() as cs:
                await cs.execute(sql_text(
                    "DELETE FROM brain.graph_edges WHERE agent_id=:aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.facts WHERE agent_id=:aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.episodes WHERE agent_id=:aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM nous_system.agents WHERE id=:aid"
                ), {"aid": agent_id})
                await cs.commit()
