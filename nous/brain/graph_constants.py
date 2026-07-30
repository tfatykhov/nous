"""Canonical shared predicates for graph and episode consumers.

Three groups live here: the graph-edge exclusion sets (below), the episode
liveness predicates, and the episode<->decision correlation window. Each was
added because the same rule had drifted across independent call sites.

--- graph-edge exclusion sets ---

The 2026-06-13 audit (Theme 6) found the relation-exclusion logic had drifted
across the graph consumers — each excluded a different subset, so every new edge
relation leaked through whichever consumer forgot it (supersedes was fixed in
PR #518; contradicts / co_occurred / happened_before still leaked). These
constants centralise the rule so a future relation is a single-site edit.

Two sets, because the correct exclusion depends on consumer PURPOSE:

- **Auto-behavior consumers** — the auto-spreading density gate, spreading
  traversal, orphan detection, cluster discovery — must not be *driven* by edges
  that are not associative connectivity: lineage (``supersedes``), negative
  (``contradicts``), temporal (``happened_before``), or builder-generated
  co-occurrence (``co_occurred`` / ``co_mention``, whose builder flags can flip
  silently and change behaviour). Counting these can flip auto-spreading on or
  strand facts from densification.

- **Retrieval neighbour expansion** (``brain._neighbors``) must not surface
  *lineage* or *negative* edges as connectivity. ``co_occurred`` / ``co_mention``
  ARE legitimate associative connectivity for retrieval, so they are NOT excluded
  there — changing that is a live ranking decision (deferred, eval-gated).
"""

from __future__ import annotations

from collections.abc import Iterable

# --- auto-behavior consumers (density gate, spreading, orphan, cluster) ---
AUTOBEHAVIOR_EXCLUDED_RELATIONS: frozenset[str] = frozenset(
    {"supersedes", "contradicts", "happened_before", "co_occurred"}
)
# co_mention edges carry relation='related_to', so they're excluded by
# extraction_method, not relation name.
AUTOBEHAVIOR_EXCLUDED_METHODS: frozenset[str] = frozenset({"co_mention"})

# --- retrieval neighbour expansion (lineage + negative only) ---
RETRIEVAL_EXCLUDED_RELATIONS: frozenset[str] = frozenset({"supersedes", "contradicts"})


def _sql_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


def autobehavior_exclusion_sql(col_prefix: str = "") -> str:
    """SQL boolean fragment excluding non-associative edges from auto-behavior
    consumers. ``col_prefix`` e.g. ``"e."`` for an aliased ``graph_edges`` table.
    Values are fixed literals (no user input), so interpolation is safe."""
    p = col_prefix
    rels = _sql_list(AUTOBEHAVIOR_EXCLUDED_RELATIONS)
    methods = _sql_list(AUTOBEHAVIOR_EXCLUDED_METHODS)
    return (
        f"{p}relation NOT IN ({rels}) "
        f"AND ({p}extraction_method IS NULL OR {p}extraction_method NOT IN ({methods}))"
    )


# --- episode lifecycle vs deletion (2026-07-12 F053 audit) ---
# `heart.episodes.active` is OVERLOADED: on facts/procedures `active=false`
# means soft-deleted, but on episodes it is the normal CLOSED lifecycle
# state (008.3 — `Episode._end()` sets it on every session close). Graph
# consumers that treat `active=false` as "dead node" erase the episode
# graph layer (prod 2026-07-12: 657 closed episodes held 6 edges). HT-1
# hit the same trap in episode search; these fragments mirror its fixed
# predicate (heart/episodes.py::search). Genuinely-deleted episodes are
# only: trivial discards (deactivated without ever ending) and F060.2
# abandoned marks.


def episode_dead_sql(col_prefix: str = "") -> str:
    """SQL boolean fragment selecting genuinely-deleted episodes only."""
    p = col_prefix
    return (
        f"(({p}active = false AND {p}ended_at IS NULL) "
        f"OR {p}outcome = 'abandoned')"
    )


def episode_live_sql(col_prefix: str = "") -> str:
    """Complement of :func:`episode_dead_sql` for row selection: ongoing
    or genuinely-closed episodes, excluding abandoned."""
    p = col_prefix
    return (
        f"(({p}active = true OR {p}ended_at IS NOT NULL) "
        f"AND {p}outcome IS DISTINCT FROM 'abandoned')"
    )


# --- episode <-> decision correlation (2026-07-28) ---
# Replaces the `heart.episode_decisions` join table (dropped in migration
# 068), which had a full write API but no runtime writer — every reader saw
# an empty table. Episode and decision are correlated instead through the
# session they share: `heart.episodes.session_id` and
# `brain.decisions.session_id`.
#
# Measured on all 87 matchable prod decisions (2026-07-28): 87 pairs,
# 87/87 covered, 0 ambiguous. Two terms are load-bearing and were each
# falsified by a prod measurement:
#
#   - The `agent_id` equality. `session_id` is NOT agent-namespaced
#     (`heartbeat-<hex>` / `subtask-<hex>` are generated identically for
#     every agent), so without it a same-named session on another agent
#     materialises a cross-agent pair.
#   - The 60s grace on the lower bound. `pre_turn` records a deliberation
#     decision at step 4 and creates the episode at step 5, so a decision
#     can predate its own episode by up to 2.2s (median 0.4s). A strict
#     `>= started_at` window loses 35 of the 87.
#
# A plain equality join with no time window is also wrong: it is a
# cross-product over multi-episode sessions (148 pairs, 15 ambiguous).
#
# Open episodes are additionally bounded by the next episode's start in the
# same session, so a session that is reused after a stuck-open episode does
# not vacuum up every later decision. Verified a no-op on prod today (still
# 87 pairs) — it is hardening for the 8 currently-open session-bearing
# episodes. Consecutive episodes overlap by the grace interval, so a
# decision in that seam can match both; readers want episode -> decision
# SET membership, for which that is benign.

EPISODE_DECISION_GRACE_SECONDS = 60


def episode_decision_bounds_sql(*, agent_param: str = "agent_id") -> str:
    """Bare SELECT over ``heart.episodes`` adding the decision-window
    columns. Wrap in parentheses as a derived table; never used alone."""
    return f"""
        SELECT id, agent_id, session_id, started_at, ended_at, active, outcome,
               -- Lower bound. The grace catches a deliberation decision
               -- recorded at pre_turn step 4, before this episode is created
               -- at step 5. But it must never reach back into a predecessor
               -- that was still running: a decision made while the previous
               -- episode was genuinely open belongs to THAT episode, not to
               -- this one's grace band. Clamp to just after the predecessor's
               -- real end. (Postgres GREATEST/LEAST ignore NULLs, so a first
               -- episode -- or one whose predecessor never closed -- simply
               -- keeps the plain grace.)
               GREATEST(
                   started_at - interval '{EPISODE_DECISION_GRACE_SECONDS} seconds',
                   LAG(ended_at) OVER (
                       PARTITION BY agent_id, session_id ORDER BY started_at
                   ) + interval '1 microsecond'
               ) AS decision_window_start,
               -- Upper bound. A CLOSED episode is authoritative: ended_at is
               -- when it actually stopped, so never truncate it (codex r2 --
               -- the r1 form applied the LEAD bound to closed predecessors
               -- too, so a session reused <60s after a normal close lost its
               -- final minute of decisions to the successor, and two episodes
               -- starting <60s apart produced a window ending before its own
               -- start). Only a STUCK-OPEN episode needs bounding, and it
               -- cedes the grace band to its successor (codex r1): a decision
               -- in that band is the successor's own pre-episode deliberation
               -- decision. The microsecond step makes the two windows tile
               -- exactly rather than share their boundary instant.
               COALESCE(
                   ended_at,
                   LEAST(
                       now(),
                       LEAD(started_at) OVER (
                           PARTITION BY agent_id, session_id ORDER BY started_at
                       ) - interval '{EPISODE_DECISION_GRACE_SECONDS} seconds'
                         - interval '1 microsecond'
                   )
               ) AS decision_window_end
        FROM heart.episodes
        WHERE session_id IS NOT NULL AND agent_id = :{agent_param}
    """


def episode_decision_join_sql(ep_prefix: str = "e.", dec_prefix: str = "d.") -> str:
    """ON-clause joining ``brain.decisions`` to the derived table produced by
    :func:`episode_decision_bounds_sql`."""
    e, d = ep_prefix, dec_prefix
    return (
        f"{d}session_id = {e}session_id "
        f"AND {d}agent_id = {e}agent_id "
        f"AND {d}created_at >= {e}decision_window_start "
        f"AND {d}created_at <= {e}decision_window_end"
    )


def episode_decisions_query(
    columns: str,
    *,
    agent_param: str = "agent_id",
    episode_param: str = "episode_id",
) -> str:
    """Full SELECT of ``columns`` from the decisions of ONE episode, oldest
    first. ``columns`` may reference ``d.`` (decision) and ``e.`` (episode)."""
    return f"""
        SELECT {columns}
        FROM ({episode_decision_bounds_sql(agent_param=agent_param)}) e
        JOIN brain.decisions d ON {episode_decision_join_sql()}
        WHERE e.id = :{episode_param}
        ORDER BY d.created_at
    """
