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
        mock_session.execute.assert_awaited_once()
        mock_session.commit.assert_awaited_once()
        assert sleep_stats["dead_edges_pruned"] == 3

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
        call = mock_session.execute.await_args
        params = call.args[1]
        assert params["agent_id"] == handler._settings.agent_id
        assert params["max_per_cycle"] == 42

    @pytest.mark.asyncio
    async def test_sql_does_not_reference_decisions_active(self):
        """Regression: brain.decisions has no `active` column; the SQL
        must NOT reference it. Caught by code review on first pass."""
        handler, mock_session = _make_handler()
        await handler._phase_prune_dead_edges({})
        call = mock_session.execute.await_args
        sql_str = str(call.args[0])
        # Either the SQL doesn't mention decisions at all, or if it does
        # (in a comment), it must not pair them with `active`.
        decisions_block = "FROM brain.decisions" in sql_str
        active_on_decisions = "decisions" in sql_str.lower() and (
            "decisions" in sql_str.split("active = false")[0].split("FROM ")[-1]
            if "active = false" in sql_str
            else False
        )
        assert not decisions_block, (
            "F053 SQL references brain.decisions in a FROM/WHERE clause; "
            "decisions has no active column today. Remove the branch."
        )

    @pytest.mark.asyncio
    async def test_sql_preserves_supersedes_lineage(self):
        """2026-06-13 audit: supersedes edges point AT the superseded (inactive)
        fact by design. The prune SQL must exclude them, or it would delete the
        lineage edges the edge-persistence fix just wrote (the original is
        deactivated in the same sleep cycle)."""
        handler, mock_session = _make_handler()
        await handler._phase_prune_dead_edges({})
        sql_str = str(mock_session.execute.await_args.args[0])
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
