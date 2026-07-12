"""Episode-liveness SQL predicates (2026-07-12 F053 audit).

`heart.episodes.active` is overloaded: `active=false` is the normal CLOSED
lifecycle state (008.3), not a deletion marker. These predicates are the
single source of truth for which episodes count as dead nodes in graph
consumers. Unit tests pin the SQL text; the integration tests in
test_f053_dead_edge_prune.py / test_graph_densifier.py verify behavior
against real Postgres.
"""

from __future__ import annotations

from nous.brain.graph_constants import episode_dead_sql, episode_live_sql


class TestEpisodeLivenessSql:
    def test_dead_sql_selects_trivial_discards_and_abandoned_only(self):
        sql = episode_dead_sql()
        assert "active = false AND ended_at IS NULL" in sql
        assert "outcome = 'abandoned'" in sql

    def test_dead_sql_does_not_treat_bare_inactive_as_dead(self):
        """The bug under fix: bare `active = false` must NOT appear as a
        standalone dead-condition — it must always be conjoined with
        `ended_at IS NULL`."""
        sql = episode_dead_sql()
        # Every occurrence of `active = false` is followed by the
        # ended_at conjunction.
        for fragment in sql.split("active = false")[1:]:
            assert fragment.lstrip().startswith("AND"), sql

    def test_live_sql_mirrors_ht1_search_predicate(self):
        sql = episode_live_sql()
        assert "active = true OR" in sql
        assert "ended_at IS NOT NULL" in sql
        assert "outcome IS DISTINCT FROM 'abandoned'" in sql

    def test_col_prefix_is_applied_to_every_column(self):
        sql = episode_dead_sql("ep.")
        assert "ep.active" in sql and "ep.ended_at" in sql and "ep.outcome" in sql
        assert " active" not in sql.replace("ep.active", "")
        sql_live = episode_live_sql("t.")
        assert "t.active" in sql_live and "t.ended_at" in sql_live and "t.outcome" in sql_live
