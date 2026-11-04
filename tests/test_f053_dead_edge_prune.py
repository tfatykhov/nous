"""F053 — Orphan-edge sleep cleanup phase.

F031 MERGE / F027 cluster_consolidation deactivate facts but leave
the `brain.graph_edges` rows incident to those facts in place.
Spreading activation walks edges only (no `active` filter), so it
wastes per-hop activation budget on dead nodes.

The `_phase_prune_dead_edges` phase runs after `graph_densification`
each sleep cycle, deletes edges whose source or target points at an
inactive fact / episode / procedure / decision, bounded per cycle.

This module verifies the phase logic without a real Postgres:

  - flag off                                  -> phase is a no-op (returns True).
  - flag on + DB success                      -> session.execute called once,
                                                 commit called, deleted-count
                                                 propagates into sleep_stats.
  - max_per_cycle <= 0                        -> phase is a no-op (returns True).
  - DB exception                              -> caught + returns False
                                                 (phase excluded from
                                                 phases_completed downstream).
  - default Settings has flag on at 1000/cycle -> backward sanity.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous.config import Settings
from nous.events import EventBus
from nous.handlers.sleep_handler import SleepHandler


def _make_handler(
    *,
    dead_edge_pruning_enabled: bool = True,
    dead_edge_pruning_max_per_cycle: int = 1000,
    deleted_ids: list | None = None,
    raise_on_execute: bool = False,
):
    """Construct a SleepHandler with mocked heart.db.session yielding a CM."""
    brain = AsyncMock()
    heart = AsyncMock()
    settings = Settings(_env_file=None)
    object.__setattr__(
        settings, "dead_edge_pruning_enabled", dead_edge_pruning_enabled
    )
    object.__setattr__(
        settings,
        "dead_edge_pruning_max_per_cycle",
        dead_edge_pruning_max_per_cycle,
    )
    bus = MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    llm_client = AsyncMock()

    # Build the async-context-manager mock for heart.db.session().
    mock_session = MagicMock()
    if raise_on_execute:
        mock_session.execute = AsyncMock(side_effect=RuntimeError("db boom"))
    else:
        result = MagicMock()
        result.all = MagicMock(
            return_value=[MagicMock(id=i) for i in (deleted_ids or [])]
        )
        mock_session.execute = AsyncMock(return_value=result)
    mock_session.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=False)
    heart.db = MagicMock()
    heart.db.session = MagicMock(return_value=cm)

    handler = SleepHandler(brain, heart, settings, bus, llm_client)
    return handler, mock_session


class TestF053DeadEdgePrune:
    @pytest.mark.asyncio
    async def test_default_settings_have_flag_on_at_1000(self):
        s = Settings(_env_file=None)
        assert s.dead_edge_pruning_enabled is True
        assert s.dead_edge_pruning_max_per_cycle == 1000

    @pytest.mark.asyncio
    async def test_flag_off_is_noop(self):
        handler, mock_session = _make_handler(dead_edge_pruning_enabled=False)
        sleep_stats = {}
        result = await handler._phase_prune_dead_edges(sleep_stats)
        assert result is True
        mock_session.execute.assert_not_called()
        assert "dead_edges_pruned" not in sleep_stats

    @pytest.mark.asyncio
    async def test_max_per_cycle_zero_is_noop(self):
        handler, mock_session = _make_handler(dead_edge_pruning_max_per_cycle=0)
        sleep_stats = {}
        result = await handler._phase_prune_dead_edges(sleep_stats)
        assert result is True
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_per_cycle_negative_is_noop(self):
        handler, mock_session = _make_handler(
            dead_edge_pruning_max_per_cycle=-5
        )
        sleep_stats = {}
        result = await handler._phase_prune_dead_edges(sleep_stats)
        assert result is True
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_prune_records_count(self):
        deleted = [uuid4(), uuid4(), uuid4()]
        handler, mock_session = _make_handler(deleted_ids=deleted)
        sleep_stats = {}
        result = await handler._phase_prune_dead_edges(sleep_stats)
        assert result is True
        # Two statements (inactive endpoint, then missing endpoint row), one commit.
        assert mock_session.execute.await_count == 2
        mock_session.commit.assert_awaited_once()
        assert sleep_stats["dead_edges_pruned"] == 3
        assert sleep_stats["dead_edges_missing_endpoint"] == 3

    @pytest.mark.asyncio
    async def test_prune_zero_records_zero(self):
        handler, mock_session = _make_handler(deleted_ids=[])
        sleep_stats = {}
        result = await handler._phase_prune_dead_edges(sleep_stats)
        assert result is True
        assert sleep_stats["dead_edges_pruned"] == 0

    @pytest.mark.asyncio
    async def test_db_exception_returns_false(self):
        handler, mock_session = _make_handler(raise_on_execute=True)
        sleep_stats = {}
        result = await handler._phase_prune_dead_edges(sleep_stats)
        assert result is False
        # commit MUST NOT happen on error
        mock_session.commit.assert_not_called()
        # Phase should not silently report success; sleep_stats untouched.
        assert "dead_edges_pruned" not in sleep_stats

    @pytest.mark.asyncio
    async def test_query_uses_agent_id_and_max_per_cycle(self):
        """Regression: SQL must bind agent_id + max_per_cycle params."""
        handler, mock_session = _make_handler(
            dead_edge_pruning_max_per_cycle=42
        )
        await handler._phase_prune_dead_edges({})
        # Nothing deleted by the first statement, so the second gets the whole cap.
        for call in mock_session.execute.await_args_list:
            params = call.args[1]
            assert params["agent_id"] == handler._settings.agent_id
            assert params["max_per_cycle"] == 42

    @pytest.mark.asyncio
    async def test_sql_does_not_reference_decisions_active(self):
        """Regression: brain.decisions has no `active` column; the SQL
        must NOT reference it. Caught by code review on first pass."""
        handler, mock_session = _make_handler()
        await handler._phase_prune_dead_edges({})
        calls = mock_session.execute.await_args_list
        # The inactive-endpoint pool has no decisions branch.
        assert "FROM brain.decisions" not in str(calls[0].args[0]), (
            "F053 inactive-node pool references brain.decisions; "
            "decisions has no active column today. Remove the branch."
        )
        # The missing-endpoint statement reads brain.decisions by id only.
        decisions_active = re.compile(r"FROM brain\.decisions\b[^)]*\bactive\b")
        for call in calls:
            assert not decisions_active.search(str(call.args[0])), (
                "brain.decisions has no active column"
            )

    @pytest.mark.asyncio
    async def test_sql_preserves_supersedes_lineage(self):
        """2026-06-13 audit: supersedes edges point AT the superseded (inactive)
        fact by design. The prune SQL must exclude them, or it would delete the
        lineage edges the edge-persistence fix just wrote (the original is
        deactivated in the same sleep cycle)."""
        handler, mock_session = _make_handler()
        await handler._phase_prune_dead_edges({})
        sql_str = str(mock_session.execute.await_args_list[0].args[0])
        assert "supersedes" in sql_str, (
            "F053 prune SQL must exclude relation = 'supersedes' so it does not "
            "delete supersession lineage edges incident to the inactive fact"
        )

    @pytest.mark.asyncio
    async def test_db_exception_records_error_type(self):
        """P2 review fix: persist exception type into sleep_stats so
        observability dashboards can detect silent regressions."""
        handler, mock_session = _make_handler(raise_on_execute=True)
        sleep_stats = {}
        result = await handler._phase_prune_dead_edges(sleep_stats)
        assert result is False
        assert sleep_stats.get("dead_edges_prune_error") == "RuntimeError"

    @pytest.mark.asyncio
    async def test_sql_does_not_treat_closed_episodes_as_dead(self):
        """2026-07-12 audit: episodes.active=false is the normal CLOSED
        lifecycle state (008.3), not deletion. The old predicate erased the
        entire episode graph layer (657 closed prod episodes held 6 edges).
        The episode branch must select only trivial discards
        (active=false AND ended_at IS NULL) and abandoned marks."""
        handler, mock_session = _make_handler()
        await handler._phase_prune_dead_edges({})
        sql_str = str(mock_session.execute.await_args_list[0].args[0])
        assert "ended_at IS NULL" in sql_str, (
            "episode dead-node branch must require ended_at IS NULL "
            "alongside active=false (trivial-discard shape)"
        )
        assert "outcome = 'abandoned'" in sql_str, (
            "episode dead-node branch must include F060.2 abandoned marks"
        )

    @pytest.mark.asyncio
    async def test_missing_endpoint_statement_gets_the_rest_of_the_cap(self):
        """K-14: the missing-endpoint statement shares the per-cycle cap with
        the inactive-endpoint one: 3 deleted of 5 leaves it a limit of 2."""
        handler, mock_session = _make_handler(
            dead_edge_pruning_max_per_cycle=5, deleted_ids=[uuid4() for _ in range(3)],
        )
        await handler._phase_prune_dead_edges({})
        calls = mock_session.execute.await_args_list
        assert [c.args[1]["max_per_cycle"] for c in calls] == [5, 2]

    @pytest.mark.asyncio
    async def test_missing_endpoint_statement_skipped_when_cap_spent(self):
        handler, mock_session = _make_handler(
            dead_edge_pruning_max_per_cycle=3, deleted_ids=[uuid4() for _ in range(3)],
        )
        sleep_stats: dict = {}
        assert await handler._phase_prune_dead_edges(sleep_stats) is True
        assert mock_session.execute.await_count == 1
        assert sleep_stats["dead_edges_pruned"] == 3
        assert sleep_stats["dead_edges_missing_endpoint"] == 0


# ===========================================================================
# Integration test — real Postgres, end-to-end behavior
# ===========================================================================
#
# Validates that the production SQL inside _phase_prune_dead_edges actually
# deletes the right edges against a real Postgres instance. Mock-based tests
# above exercise the Python control flow but don't catch SQL drift (e.g. a
# missing column, a wrong type cast, a CTE that doesn't return what the
# DELETE expects). This test inserts a known fixture, calls the phase, and
# asserts the DB state changed correctly.
#
# Skip rules:
#   - @pytest.mark.integration  → only runs with --integration flag
#   - @pytest.mark.postgres_only → only runs with NOUS_TEST_DB=postgres
# Both must be present; on a clean dev box this test stays silent.
#
# Run via:
#   NOUS_TEST_DB=postgres uv run pytest tests/test_f053_dead_edge_prune.py \
#     -m integration --integration -v


@pytest.mark.integration
@pytest.mark.postgres_only
class TestF053Integration:
    """End-to-end F053 against real Postgres.

    The session fixture (tests/conftest.py) wraps each test in a
    transaction that rolls back on exit, so this test never mutates
    the dev DB beyond the test scope.
    """

    @pytest.mark.asyncio
    async def test_phase_deletes_only_inactive_endpoint_edges(
        self, db, mock_embeddings,
    ):
        """Insert known fixture: 2 active facts + 2 inactive facts +
        4 edges (2 between active-only, 2 incident to inactive). Run
        the phase. Verify only the 2 dead edges were deleted; the 2
        active-active edges survive.

        The handler opens its OWN session from the connection pool, so
        the rollback-fixture pattern (tests/conftest.py::session) is
        not usable here — the handler's session wouldn't see the
        fixture-session's uncommitted data. Instead: commit fixture data
        with a unique agent_id and DELETE everything in a finally block.
        """
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy import text as sql_text

        from nous.config import Settings
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler

        agent_id = f"f053-it-{uuid4().hex[:8]}"

        # Fixture: 2 active + 2 inactive facts; 4 edges.
        f_active_a = uuid4()
        f_active_b = uuid4()
        f_inactive_a = uuid4()
        f_inactive_b = uuid4()
        e_alive_1 = uuid4()
        e_alive_2 = uuid4()
        e_dead_src = uuid4()
        e_dead_tgt = uuid4()
        e_supersedes = uuid4()  # points at inactive fact but must SURVIVE

        try:
            # Insert and commit fixture so the handler's own session sees it.
            async with db.session() as fs:
                await fs.execute(sql_text(
                    "INSERT INTO nous_system.agents (id, name) "
                    "VALUES (:aid, :name) ON CONFLICT (id) DO NOTHING"
                ), {"aid": agent_id, "name": "F053 IT agent"})
                for fid, active in [
                    (f_active_a, True), (f_active_b, True),
                    (f_inactive_a, False), (f_inactive_b, False),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.facts "
                        "(id, agent_id, content, active, created_at) "
                        "VALUES (:id, :aid, :c, :a, :t)"
                    ), {
                        "id": fid, "aid": agent_id,
                        "c": f"f053 fixture {fid}", "a": active,
                        "t": datetime.now(UTC),
                    })
                # Edges:
                # - e_alive_1, e_alive_2: between active facts (SURVIVE)
                # - e_dead_src: from inactive → active (DELETE)
                # - e_dead_tgt: from active → inactive (DELETE)
                edges = [
                    (e_alive_1, f_active_a, "fact", f_active_b, "fact", "related_to"),
                    (e_alive_2, f_active_b, "fact", f_active_a, "fact", "informed_by"),
                    (e_dead_src, f_inactive_a, "fact", f_active_a, "fact", "supports"),
                    (e_dead_tgt, f_active_a, "fact", f_inactive_b, "fact", "evidence_for"),
                    # supersedes lineage: active → inactive, must NOT be pruned.
                    (e_supersedes, f_active_a, "fact", f_inactive_a, "fact", "supersedes"),
                ]
                for eid, src, src_t, tgt, tgt_t, rel in edges:
                    await fs.execute(sql_text(
                        "INSERT INTO brain.graph_edges "
                        "(id, source_id, source_type, target_id, target_type, "
                        " agent_id, relation, weight) "
                        "VALUES (:id, :s, :st, :t, :tt, :aid, :rel, 1.0)"
                    ), {
                        "id": eid, "s": src, "st": src_t,
                        "t": tgt, "tt": tgt_t, "aid": agent_id, "rel": rel,
                    })
                await fs.commit()

            # Build the real SleepHandler.
            from unittest.mock import AsyncMock, MagicMock
            from nous.heart import Heart

            settings = Settings()
            object.__setattr__(settings, "agent_id", agent_id)
            object.__setattr__(settings, "dead_edge_pruning_enabled", True)
            object.__setattr__(settings, "dead_edge_pruning_max_per_cycle", 1000)

            heart = Heart(db, settings, embedding_provider=mock_embeddings)
            brain = AsyncMock()
            bus = MagicMock(spec=EventBus)
            bus.on = MagicMock()
            bus.emit = AsyncMock()
            llm_client = AsyncMock()

            handler = SleepHandler(brain, heart, settings, bus, llm_client)

            sleep_stats: dict = {}
            result = await handler._phase_prune_dead_edges(sleep_stats)

            assert result is True
            # Only the 2 non-lineage dead edges are pruned; the supersedes
            # lineage edge survives despite pointing at an inactive fact.
            assert sleep_stats.get("dead_edges_pruned") == 2

            # Verify the 2 active-active edges + the supersedes lineage survive.
            async with db.session() as vs:
                rows = await vs.execute(sql_text(
                    "SELECT id FROM brain.graph_edges WHERE agent_id = :aid"
                ), {"aid": agent_id})
                surviving_ids = {r.id for r in rows}
            assert surviving_ids == {e_alive_1, e_alive_2, e_supersedes}, (
                f"expected 2 alive + 1 supersedes lineage edge, got {surviving_ids}"
            )

            await heart.close()
        finally:
            # Always clean up the fixture rows, even on assertion failure.
            async with db.session() as cs:
                await cs.execute(sql_text(
                    "DELETE FROM brain.graph_edges WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.facts WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM nous_system.agents WHERE id = :aid"
                ), {"aid": agent_id})
                await cs.commit()

    @pytest.mark.asyncio
    async def test_closed_episode_edges_survive_prune(
        self, db, mock_embeddings,
    ):
        """2026-07-12 audit: fixture has one normally-closed episode
        (active=false, ended_at set, outcome='success'), one trivial-discard
        episode (active=false, ended_at NULL), one abandoned episode
        (prod shape per F060.2: ended_at SET, outcome='abandoned'), each
        with one incident edge from an active fact. Run the phase. Only the
        trivial + abandoned episodes' edges are deleted; the closed
        episode's edge survives."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from sqlalchemy import text as sql_text

        from nous.config import Settings
        from nous.events import EventBus
        from nous.handlers.sleep_handler import SleepHandler
        from nous.heart import Heart

        agent_id = f"f053-ep-{uuid4().hex[:8]}"
        now = datetime.now(UTC)

        fact_id = uuid4()
        ep_closed = uuid4()
        ep_trivial = uuid4()
        ep_abandoned = uuid4()
        e_closed = uuid4()      # SURVIVES
        e_trivial = uuid4()     # DELETED
        e_abandoned = uuid4()   # DELETED

        try:
            async with db.session() as fs:
                await fs.execute(sql_text(
                    "INSERT INTO nous_system.agents (id, name) "
                    "VALUES (:aid, :name) ON CONFLICT (id) DO NOTHING"
                ), {"aid": agent_id, "name": "F053 episode IT agent"})
                await fs.execute(sql_text(
                    "INSERT INTO heart.facts (id, agent_id, content, active) "
                    "VALUES (:id, :aid, 'anchor fact', true)"
                ), {"id": fact_id, "aid": agent_id})
                episodes = [
                    # (id, active, ended_at, outcome)
                    (ep_closed, False, now, "success"),
                    (ep_trivial, False, None, None),
                    # Prod shape (F060.2 sets ended_at=COALESCE(ended_at, now())):
                    # ended_at SET — dead only via the outcome='abandoned' branch.
                    (ep_abandoned, False, now, "abandoned"),
                ]
                for eid, active, ended, outcome in episodes:
                    await fs.execute(sql_text(
                        "INSERT INTO heart.episodes "
                        "(id, agent_id, title, summary, active, started_at, "
                        " ended_at, outcome) "
                        "VALUES (:id, :aid, 'f053 ep fixture', 'summary', "
                        "        :act, :st, :en, :oc)"
                    ), {
                        "id": eid, "aid": agent_id, "act": active,
                        "st": now, "en": ended, "oc": outcome,
                    })
                for eid, ep in [
                    (e_closed, ep_closed),
                    (e_trivial, ep_trivial),
                    (e_abandoned, ep_abandoned),
                ]:
                    await fs.execute(sql_text(
                        "INSERT INTO brain.graph_edges "
                        "(id, source_id, source_type, target_id, target_type, "
                        " agent_id, relation, weight) "
                        "VALUES (:id, :s, 'fact', :t, 'episode', :aid, "
                        "        'extracted_from', 1.0)"
                    ), {"id": eid, "s": fact_id, "t": ep, "aid": agent_id})
                await fs.commit()

            settings = Settings()
            object.__setattr__(settings, "agent_id", agent_id)
            object.__setattr__(settings, "dead_edge_pruning_enabled", True)
            object.__setattr__(settings, "dead_edge_pruning_max_per_cycle", 1000)

            heart = Heart(db, settings, embedding_provider=mock_embeddings)
            brain = AsyncMock()
            bus = MagicMock(spec=EventBus)
            bus.on = MagicMock()
            bus.emit = AsyncMock()
            handler = SleepHandler(brain, heart, settings, bus, AsyncMock())

            sleep_stats: dict = {}
            assert await handler._phase_prune_dead_edges(sleep_stats) is True
            assert sleep_stats.get("dead_edges_pruned") == 2

            async with db.session() as vs:
                rows = await vs.execute(sql_text(
                    "SELECT id FROM brain.graph_edges WHERE agent_id = :aid"
                ), {"aid": agent_id})
                surviving = {r.id for r in rows}
            assert surviving == {e_closed}, (
                f"closed episode's edge must survive; got {surviving}"
            )
            await heart.close()
        finally:
            async with db.session() as cs:
                await cs.execute(sql_text(
                    "DELETE FROM brain.graph_edges WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.facts WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM heart.episodes WHERE agent_id = :aid"
                ), {"aid": agent_id})
                await cs.execute(sql_text(
                    "DELETE FROM nous_system.agents WHERE id = :aid"
                ), {"aid": agent_id})
                await cs.commit()


# ===========================================================================
# K-14 / L-4: edges whose endpoint ROW is missing (hard-deleted out of band)
# ===========================================================================


async def _insert_live_node(fs, node_type: str, agent_id: str):
    """Insert one live node of ``node_type`` for ``agent_id``; return its id."""
    from datetime import UTC, datetime

    from sqlalchemy import text as sql_text

    nid = uuid4()
    if node_type == "fact":
        await fs.execute(sql_text(
            "INSERT INTO heart.facts (id, agent_id, content, active) "
            "VALUES (:id, :a, 'k14 live fact', true)"
        ), {"id": nid, "a": agent_id})
    elif node_type == "episode":
        await fs.execute(sql_text(
            "INSERT INTO heart.episodes (id, agent_id, summary, active, started_at) "
            "VALUES (:id, :a, 'k14 live episode', true, :t)"
        ), {"id": nid, "a": agent_id, "t": datetime.now(UTC)})
    elif node_type == "procedure":
        await fs.execute(sql_text(
            "INSERT INTO heart.procedures (id, agent_id, name, active) "
            "VALUES (:id, :a, 'k14 live procedure', true)"
        ), {"id": nid, "a": agent_id})
    elif node_type == "decision":
        await fs.execute(sql_text(
            "INSERT INTO brain.decisions "
            "(id, agent_id, description, confidence, category, stakes) "
            "VALUES (:id, :a, 'k14 live decision', 0.8, 'architecture', 'low')"
        ), {"id": nid, "a": agent_id})
    elif node_type == "chunk":
        ep = await _insert_live_node(fs, "episode", agent_id)
        await fs.execute(sql_text(
            "INSERT INTO heart.episode_chunks (id, agent_id, episode_id, chunk_index, content) "
            "VALUES (:id, :a, :ep, 0, 'k14 live chunk')"
        ), {"id": nid, "a": agent_id, "ep": ep})
    else:  # pragma: no cover - test helper misuse
        raise ValueError(node_type)
    return nid


async def _insert_edge_row(fs, agent_id, src, src_t, tgt, tgt_t, relation="related_to"):
    from sqlalchemy import text as sql_text

    eid = uuid4()
    await fs.execute(sql_text(
        "INSERT INTO brain.graph_edges "
        "(id, source_id, source_type, target_id, target_type, agent_id, relation, weight) "
        "VALUES (:id, :s, :st, :t, :tt, :a, :rel, 1.0)"
    ), {"id": eid, "s": src, "st": src_t, "t": tgt, "tt": tgt_t, "a": agent_id, "rel": relation})
    return eid


async def _run_prune(db, mock_embeddings, agent_id: str, cap: int = 1000) -> dict:
    from unittest.mock import AsyncMock, MagicMock

    from nous.heart import Heart

    settings = Settings()
    object.__setattr__(settings, "agent_id", agent_id)
    object.__setattr__(settings, "dead_edge_pruning_enabled", True)
    object.__setattr__(settings, "dead_edge_pruning_max_per_cycle", cap)
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    bus = MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    handler = SleepHandler(AsyncMock(), heart, settings, bus, AsyncMock())
    sleep_stats: dict = {}
    try:
        assert await handler._phase_prune_dead_edges(sleep_stats) is True
    finally:
        await heart.close()
    return sleep_stats


async def _edge_ids(db, agent_id: str) -> set:
    from sqlalchemy import text as sql_text

    async with db.session() as vs:
        rows = await vs.execute(sql_text(
            "SELECT id FROM brain.graph_edges WHERE agent_id = :a"
        ), {"a": agent_id})
        return {r.id for r in rows}


async def _cleanup(db, *agent_ids: str) -> None:
    from sqlalchemy import text as sql_text

    async with db.session() as cs:
        for aid in agent_ids:
            await cs.execute(sql_text("DELETE FROM brain.graph_edges WHERE agent_id = :a"), {"a": aid})
            await cs.execute(sql_text("DELETE FROM heart.episode_chunks WHERE agent_id = :a"), {"a": aid})
            await cs.execute(sql_text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": aid})
            await cs.execute(sql_text("DELETE FROM heart.episodes WHERE agent_id = :a"), {"a": aid})
            await cs.execute(sql_text("DELETE FROM heart.procedures WHERE agent_id = :a"), {"a": aid})
            await cs.execute(sql_text("DELETE FROM brain.decisions WHERE agent_id = :a"), {"a": aid})
        await cs.commit()


@pytest.mark.postgres_only
class TestF053MissingEndpointIntegration:
    """K-14: F053 also deletes edges whose endpoint row does not exist at all.
    The inactive-node pool can never match those (there is no row to be
    inactive), so before this branch they survived every cycle.

    Marked ``postgres_only`` but not ``integration``: CI runs on Postgres
    without ``--integration``, and these tests clean up their own agents."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("node_type", ["fact", "episode", "procedure", "decision", "chunk"])
    async def test_prunes_edge_with_missing_endpoint_of_each_type(
        self, db, mock_embeddings, node_type,
    ):
        agent_id = f"k14-{node_type}-{uuid4().hex[:8]}"
        try:
            async with db.session() as fs:
                a = await _insert_live_node(fs, "fact", agent_id)
                b = await _insert_live_node(fs, "fact", agent_id)
                live = await _insert_live_node(fs, node_type, agent_id)
                keep_ff = await _insert_edge_row(fs, agent_id, a, "fact", b, "fact")
                keep_live = await _insert_edge_row(fs, agent_id, a, "fact", live, node_type)
                # Endpoint ids with no row in the type's table.
                await _insert_edge_row(fs, agent_id, uuid4(), node_type, a, "fact")
                await _insert_edge_row(fs, agent_id, b, "fact", uuid4(), node_type)
                await fs.commit()

            stats = await _run_prune(db, mock_embeddings, agent_id)

            assert await _edge_ids(db, agent_id) == {keep_ff, keep_live}
            assert stats["dead_edges_pruned"] == 0
            assert stats["dead_edges_missing_endpoint"] == 2
        finally:
            await _cleanup(db, agent_id)

    @pytest.mark.asyncio
    async def test_supersedes_edge_kept_when_rows_exist_pruned_when_row_missing(
        self, db, mock_embeddings,
    ):
        """Lineage to an INACTIVE fact is kept (the row exists); lineage to a
        fact row that no longer exists is garbage and goes."""
        from sqlalchemy import text as sql_text

        agent_id = f"k14-sup-{uuid4().hex[:8]}"
        try:
            async with db.session() as fs:
                winner = await _insert_live_node(fs, "fact", agent_id)
                loser = await _insert_live_node(fs, "fact", agent_id)
                await fs.execute(sql_text(
                    "UPDATE heart.facts SET active = false WHERE id = :id"
                ), {"id": loser})
                kept = await _insert_edge_row(
                    fs, agent_id, winner, "fact", loser, "fact", "supersedes",
                )
                await _insert_edge_row(
                    fs, agent_id, winner, "fact", uuid4(), "fact", "supersedes",
                )
                await fs.commit()

            stats = await _run_prune(db, mock_embeddings, agent_id)

            assert await _edge_ids(db, agent_id) == {kept}
            assert stats["dead_edges_pruned"] == 0
            assert stats["dead_edges_missing_endpoint"] == 1
        finally:
            await _cleanup(db, agent_id)

    @pytest.mark.asyncio
    async def test_missing_endpoint_prune_respects_shared_cap(self, db, mock_embeddings):
        """Both statements share dead_edge_pruning_max_per_cycle: with a cap of
        3 and 2 inactive-endpoint edges, only 1 missing-endpoint edge goes."""
        from sqlalchemy import text as sql_text

        agent_id = f"k14-cap-{uuid4().hex[:8]}"
        try:
            async with db.session() as fs:
                a = await _insert_live_node(fs, "fact", agent_id)
                dead = await _insert_live_node(fs, "fact", agent_id)
                await fs.execute(sql_text(
                    "UPDATE heart.facts SET active = false WHERE id = :id"
                ), {"id": dead})
                await _insert_edge_row(fs, agent_id, a, "fact", dead, "fact", "related_to")
                await _insert_edge_row(fs, agent_id, a, "fact", dead, "fact", "evidence_for")
                for _ in range(3):
                    await _insert_edge_row(fs, agent_id, a, "fact", uuid4(), "fact")
                await fs.commit()

            stats = await _run_prune(db, mock_embeddings, agent_id, cap=3)

            assert len(await _edge_ids(db, agent_id)) == 2
            assert stats["dead_edges_pruned"] == 2
            assert stats["dead_edges_missing_endpoint"] == 1
        finally:
            await _cleanup(db, agent_id)

    @pytest.mark.asyncio
    async def test_other_agent_edges_untouched(self, db, mock_embeddings):
        """The edge side is agent-scoped; the node side is a primary-key
        lookup only, so an endpoint that exists under ANOTHER agent is not
        "missing" and its edge is kept (C7)."""
        agent_id = f"k14-me-{uuid4().hex[:8]}"
        other_id = f"k14-other-{uuid4().hex[:8]}"
        try:
            async with db.session() as fs:
                mine = await _insert_live_node(fs, "fact", agent_id)
                theirs = await _insert_live_node(fs, "fact", other_id)
                to_foreign = await _insert_edge_row(fs, agent_id, mine, "fact", theirs, "fact")
                await _insert_edge_row(fs, agent_id, mine, "fact", uuid4(), "fact")
                other_ghost = await _insert_edge_row(fs, other_id, theirs, "fact", uuid4(), "fact")
                await fs.commit()

            stats = await _run_prune(db, mock_embeddings, agent_id)

            assert await _edge_ids(db, agent_id) == {to_foreign}
            assert await _edge_ids(db, other_id) == {other_ghost}
            assert stats["dead_edges_missing_endpoint"] == 1
        finally:
            await _cleanup(db, agent_id, other_id)
