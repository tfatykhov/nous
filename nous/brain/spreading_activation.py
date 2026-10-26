"""F022 Phase 4 — Spreading activation with density gate.

Provides multi-hop graph traversal using a recursive CTE when
graph density exceeds a configurable threshold.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.graph_constants import (
    associative_edge_sql,
    autobehavior_exclusion_sql,
    spreading_exclusion_sql,
)
from nous.config import Settings

logger = logging.getLogger(__name__)


async def compute_graph_density(session: AsyncSession, agent_id: str) -> float:
    """Compute average edges per unique node for the given agent.

    F076: co-mention edges (``extraction_method='co_mention'``) are EXCLUDED from
    the density count, because a BUILDER flag must never silently decide whether
    auto-mode fires — flipping ``comention_linking_enabled`` would otherwise push
    an agent over ``spreading_activation_density_threshold`` and switch decision
    retrieval into spreading activation as a side effect.
    ``IS DISTINCT FROM`` keeps NULL/legacy ``extraction_method`` rows counted.

    (The original note here justified the exclusion by saying the retrieval
    consumers "default off". That is no longer true — Path A, adjacency boost and
    seed-score are all ``true`` in prod — but the exclusion stands on the
    builder-flag argument above, which does not depend on consumer state. This
    is a DENSITY-GATE rule only: it decides whether the mechanism runs. The
    traversal in ``spreading_activation_search`` is a different question, and
    applying this same predicate there is what bars spreading from every
    associative edge in the graph.)

    2026-06-13 audit: ``supersedes``, ``contradicts``, ``happened_before``, and
    ``co_occurred`` are likewise excluded — none are real associative
    connectivity (the traversal refuses or these are lineage/temporal/builder
    edges), so counting them could push an agent over the threshold and flip
    ``auto`` mode into spreading activation unintentionally. (1e — folded into
    PR-1 so the new in-band contradiction edges can't inflate density.)
    """
    # 2b: single source of truth for the auto-behavior exclusion (graph_constants).
    excl = autobehavior_exclusion_sql()
    sql = text(f"""
        WITH node_counts AS (
            SELECT COUNT(*) AS edge_count,
                   (SELECT COUNT(DISTINCT node_id) FROM (
                       SELECT source_id AS node_id FROM brain.graph_edges
                       WHERE agent_id = :agent_id AND {excl}
                       UNION
                       SELECT target_id AS node_id FROM brain.graph_edges
                       WHERE agent_id = :agent_id AND {excl}
                   ) nodes) AS unique_nodes
            FROM brain.graph_edges
            WHERE agent_id = :agent_id AND {excl}
        )
        SELECT CASE WHEN unique_nodes = 0 THEN 0.0
                    ELSE edge_count::float / unique_nodes
               END AS density
        FROM node_counts
    """)
    result = await session.execute(sql, {"agent_id": agent_id})
    row = result.one()
    return float(row.density)


def should_use_spreading_activation(
    settings: Settings,
    cached_density: float,
) -> bool:
    """Determine whether to use spreading activation or simple 1-hop."""
    mode = settings.spreading_activation_enabled.lower()
    if mode == "true":
        return True
    if mode == "false":
        return False
    return cached_density >= settings.spreading_activation_density_threshold


def _fan_factor_sql(edge_filter_sql: str) -> str:
    """F105: the fan factor of the frontier node ``a``, as a one-row subquery.

    For ``a``'s degree ``d`` (edges where it is ``source_id`` or ``target_id``,
    this agent only, under ``edge_filter_sql``: the traversal's own eligibility
    predicate on alias ``e2``) the ``factor`` column is ``1.0`` when
    ``d <= :fan_free`` and ``(:fan_free / d) ** :fan_exp`` above it, so always
    ``<= 1``. Every ``:fan_free`` is cast to FLOAT: one bind parameter cannot be
    deduced as two types, and an integer ``:fan_free / d`` would truncate
    10 / 41 to 0. It is joined LATERAL before the edge join so that it runs
    once per frontier row; EXPLAIN on realistic graphs shows that plan. The
    planner may still evaluate it once per joined edge (it does on very small
    graphs), which changes the cost only, never the value.
    """
    return (
        "SELECT CASE WHEN COUNT(*) <= CAST(:fan_free AS FLOAT) THEN 1.0"
        " ELSE POWER(CAST(:fan_free AS FLOAT) / COUNT(*), CAST(:fan_exp AS FLOAT)) END AS factor"
        " FROM brain.graph_edges e2"
        " WHERE (e2.source_id = a.id OR e2.target_id = a.id)"
        " AND e2.agent_id = :agent_id"
        f" AND {edge_filter_sql}"
    )


# F105 Task 7: how the per-seed best paths to a node (``act.activation``, one
# row per seed) combine. noisy-OR caps each one below 1 so the logarithm is
# defined; the GREATEST is a second guard.
_OVER_NODE = "OVER (PARTITION BY act.id, act.node_type)"
_COMBINE_SQL: dict[str, str] = {
    "sat": f"LEAST(1, SUM(act.activation) {_OVER_NODE})",
    "noisy_or": f"1 - EXP(SUM(LN(GREATEST(1 - LEAST(act.activation, 0.999999), 1e-6))) {_OVER_NODE})",
}


async def spreading_activation_search(
    session: AsyncSession,
    agent_id: str,
    seed_nodes: list[tuple[UUID, str, float]],
    settings: Settings,
    limit: int = 20,
    exclude_ids: set[UUID] | None = None,
    seed_out: dict[UUID, UUID] | None = None,
    parent_out: dict[UUID, UUID] | None = None,
    rescore_ids: set[UUID] | None = None,
    rescore_out: dict[UUID, float] | None = None,
) -> list[tuple[UUID, str, float, int]]:
    """Run spreading activation CTE and return activated nodes.

    Args:
        seed_nodes: List of (node_id, node_type, score) from vector search
        limit: max activated rows returned. Callers that post-filter the
            results (e.g. the pipeline's content-resolution drop, PR #555)
            should over-fetch so dropped rows don't consume the window.
        exclude_ids: node ids the caller already has as candidates (seeds,
            direct heart/chunk hits, prior graph-stage results). Excluded
            INSIDE the final SELECT — before the LIMIT — so known
            duplicates never consume the result window (codex P2,
            PR #556). Traversal still passes THROUGH excluded nodes.
        seed_out: when given, filled with ``node_id -> seed_id``: the seed
            the WINNING path started from (the path whose activation is
            returned). F091 used to record every spreading row under the first
            seed, so the dashboard rooted all of them under whichever seed
            happened to be first. A separate out-parameter rather than a fifth
            tuple element, so callers and mocks that unpack four stay valid.
        parent_out: when given, filled with ``node_id -> parent_id``: the node
            the winning path's last hop left (a seed maps to itself). F105: the
            replay's hub share judges a depth-2 row by this via node. Only when
            it is given does the CTE carry the extra column; which path wins is
            unchanged.
        rescore_ids: F105 Task 8, the caller's existing candidates. A seed
            among them is dropped first: it is not a rescore id. The rest never
            appear in the returned list or use its ``limit``; each one the
            traversal reaches comes back through ``rescore_out``. An id also in
            ``exclude_ids`` is still a rescore id. None or empty: today's SQL.
        rescore_out: when given, filled with ``node_id -> activation`` for the
            reached ``rescore_ids``, aggregated as a returned row's is
            (``spreading_aggregation``). ``seed_out`` and ``parent_out``
            describe the returned rows only.

    Returns:
        List of ``(node_id, node_type, activation, depth)`` sorted by
        activation desc.

        Aggregation across paths is MAX (bounded best-path), not SUM: each
        path's activation is seed_score × ∏(weight × decay) ≤ 1 when weights
        ≤ 1, so MAX keeps results on the seeds' [0,1] score scale and makes
        undirected-traversal cycles score-harmless. (Plan 1.2 — SUM let
        multi-path/cyclic nodes exceed 1.0 and dominate the RRF-sorted
        merge.) The per-hop weight term is clamped to 1.0 in the CTE:
        ``brain.graph_edges.weight`` has no DB CHECK, so the bound is
        enforced here rather than assumed of every writer. F105's fan factor
        is ``<= 1`` too, so the bound holds with fan normalisation on, and so
        is the associative-edge multiplier (Task 4, ``le=1.0``).

        F105 Task 7, ``spreading_aggregation``: ``max`` (the default) is the
        above and keeps this statement byte for byte. ``sat`` and ``noisy_or``
        first keep each seed's best path to a node, so parallel edges and
        cycles from one seed still count once, then combine the seeds'
        contributions: ``min(1, sum)``, or ``1 - prod(1 - a)`` with each ``a``
        capped at 0.999999. Both stay ``<= 1``, but a node reached from several
        seeds can exceed its winning seed's score. ``depth``, ``seed_out`` and
        ``parent_out`` then come from the largest contributor's best path
        (ties: the shorter, then the lower seed id). These two modes use LEAST,
        LN and EXP, so like the fan LATERAL they run on Postgres only.

        A8: ``depth`` is the hop count of the WINNING path — the one whose
        activation survived the MAX — not ``MIN(depth)``. Those differ when a
        node is reachable both by a long strong path and a short weak one, and
        the winning path is the one the returned score actually describes.
        Implemented with ``ROW_NUMBER()`` rather than ``DISTINCT ON`` because
        the suite's default backend is SQLite (same constraint that forced
        ``CASE`` over ``LEAST`` below). Callers pass it to F091 as the real
        hop; before this, ``retrieval_pipeline`` hardcoded ``hop=2`` for every
        spreading expansion, so production telemetry could not separate a
        one-hop neighbour from a two-hop one.

        F105 Task 8, ``rescore_ids``: the ``exclude_ids`` predicate moves from
        the CTE to the final statement, which becomes two branches told apart
        by an ``is_rescore`` column: the returned rows (neither excluded nor
        rescore ids, ordered and limited as before) and the rescore rows.
        Excluding a whole node never changes another node's ``ROW_NUMBER`` or
        window sum, so the returned rows are those ``exclude_ids |
        rescore_ids`` would give. The outcome and strategy-card predicates
        stay in the CTE: the resolver drops such a node, so it is never a
        candidate with a score to raise.
    """
    if not seed_nodes:
        return []

    values_parts = []
    params: dict = {
        "decay": settings.spreading_activation_decay,
        "max_depth": settings.spreading_activation_max_depth,
        "agent_id": agent_id,
        "result_limit": int(limit),
    }
    seed_ids = {nid for nid, _ntype, _score in seed_nodes}
    rescore = {nid for nid in rescore_ids or () if nid not in seed_ids}
    conditions: list[str] = []
    if exclude_ids:
        params["excluded_ids"] = [str(x) for x in exclude_ids]
        if not rescore:
            conditions.append("act.id != ALL(CAST(:excluded_ids AS uuid[]))")

    # A6: a decision whose outcome the resolver refuses can NEVER be rendered,
    # but it still clears the activation floor and consumes a slot in the
    # caller's result window — measured 251 of ~1,891 activated candidates on
    # prod (24% of everything that survives the floor), from 309 such
    # endpoints. Push the same outcome predicate
    # `Brain._resolve_node_descriptions` applies (brain.py, the 2026-07-27
    # decision_outcome_score_factors decision) down into the CTE so the window
    # is spent on nodes that can actually reach the model.
    #
    # DELIBERATELY CONSERVATIVE — this excludes only rows the Python resolver
    # would certainly drop. Agent-scoping, missing rows, inactive facts and any
    # NULL-confidence edge case are left to fall through and be dropped there
    # as they are today, so this can lose nothing that renders now. It is an
    # optimisation of the window, not a second filtering policy.
    demoted_outcomes = [
        o for o, f in (
            getattr(settings, "decision_outcome_score_factors", {}) or {}
        ).items() if f < 1.0
    ]
    refusals = ["(d.outcome = 'failure' AND d.confidence = 0.0)"]
    if demoted_outcomes:
        placeholders = []
        for i, outcome in enumerate(demoted_outcomes):
            params[f"demoted_{i}"] = outcome
            placeholders.append(f":demoted_{i}")
        refusals.append(
            f"COALESCE(d.outcome, 'pending') IN ({', '.join(placeholders)})"
        )
    conditions.append(
        "NOT (act.node_type = 'decision' AND EXISTS ("
        "  SELECT 1 FROM brain.decisions d"
        "  WHERE d.id = act.id AND d.agent_id = :agent_id"
        f"    AND ({' OR '.join(refusals)})"
        "))"
    )

    # Same reason as A6, for strategy cards: the resolver leaves a card
    # (a procedure of the strategy kind) out unless its caller asks for cards,
    # and the caller of this search does not. Kept out here, a card no longer
    # uses a row of the result window. The traversal is unchanged: activation
    # still spreads through a card to its other neighbours. Imported here like
    # in Brain._neighbors: nous.brain does not load nous.heart.
    from nous.heart.schemas import JOB_LESSON_KIND, STRATEGY_CARD_KIND

    params["strategy_kind"] = STRATEGY_CARD_KIND
    # Job lessons are left out the same way: they belong to their own job's runs.
    params["job_lesson_kind"] = JOB_LESSON_KIND
    conditions.append(
        "NOT (act.node_type = 'procedure' AND EXISTS ("
        "  SELECT 1 FROM heart.procedures p"
        "  WHERE p.id = act.id AND p.kind IN (:strategy_kind, :job_lesson_kind)"
        "))"
    )

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    for i, (nid, ntype, score) in enumerate(seed_nodes):
        values_parts.append(f"(CAST(:id_{i} AS UUID), CAST(:type_{i} AS VARCHAR), CAST(:score_{i} AS FLOAT))")
        params[f"id_{i}"] = str(nid)
        params[f"type_{i}"] = ntype
        params[f"score_{i}"] = float(score)

    values_clause = ", ".join(values_parts)
    # 2b: by default the same auto-behavior exclusion as the density gate
    # (graph_constants). F105: one predicate for the traversal and the fan
    # degree count, so a change to what the traversal may cross changes both.
    # Task 4's flag admits associative edges to both; the density gate keeps
    # its own predicate. (The SQL comment at the traversal's WHERE describes the
    # default and stays as is: the flags-off text must not change.)
    allow_associative = bool(getattr(settings, "spreading_associative_edges_enabled", False))

    def edge_filter(col_prefix: str) -> str:
        return spreading_exclusion_sql(col_prefix, allow_associative=allow_associative)

    excl_e = edge_filter("e.")

    # F105 fragments: each is "" while its option is off, so the SQL is then
    # byte-identical to before (tests/test_spreading_sql_identity.py).
    fan_join = fan_mult = assoc_mult = ""
    if allow_associative:
        params["assoc_mult"] = float(getattr(settings, "spreading_associative_weight_mult", 0.5))
        assoc_mult = (
            f"\n                    * CASE WHEN {associative_edge_sql('e.')}"
            " THEN CAST(:assoc_mult AS FLOAT) ELSE 1.0 END"
        )
    if getattr(settings, "spreading_fan_normalisation_enabled", False):
        params["fan_free"] = int(getattr(settings, "spreading_fan_free_degree", 10))
        params["fan_exp"] = float(getattr(settings, "spreading_fan_exponent", 0.5))
        fan_join = f"\n            CROSS JOIN LATERAL ({_fan_factor_sql(edge_filter('e2.'))}) fan"
        fan_mult = "\n                    * fan.factor"
    seed_parent = step_parent = ranked_parent = final_parent = ""
    if parent_out is not None:
        seed_parent = ", id AS parent_id"
        step_parent = ",\n                a.id"
        ranked_parent = ", act.parent_id"
        final_parent = ", parent_id"

    # F105 Task 7: "max" is today's statement, byte for byte.
    aggregation = getattr(settings, "spreading_aggregation", "max")
    if aggregation == "max":
        final_cols = f"id, node_type, activation AS total_activation, depth, seed_id{final_parent}"
    else:
        final_cols = f"id, node_type, total_activation, depth, seed_id{final_parent}"
    final_sql = f"""
        SELECT {final_cols}
        FROM ranked
        WHERE rn = 1
        ORDER BY total_activation DESC
        LIMIT :result_limit"""
    if rescore:
        # F105 Task 8: parentheses bind ORDER BY and LIMIT to the first branch.
        params["rescore_ids"] = [str(x) for x in rescore]
        main_excl = " AND id != ALL(CAST(:excluded_ids AS uuid[]))" if exclude_ids else ""
        final_sql = f"""
        (SELECT {final_cols}, FALSE AS is_rescore
         FROM ranked
         WHERE rn = 1{main_excl} AND id != ALL(CAST(:rescore_ids AS uuid[]))
         ORDER BY total_activation DESC
         LIMIT :result_limit)
        UNION ALL
        (SELECT {final_cols}, TRUE AS is_rescore
         FROM ranked
         WHERE rn = 1 AND id = ANY(CAST(:rescore_ids AS uuid[])))
        ORDER BY is_rescore, total_activation DESC"""
    if aggregation == "max":
        aggregate_sql = f"""
        -- A8: keep the winning path's depth alongside its activation.
        -- ROW_NUMBER over (activation DESC, depth ASC) selects exactly the row
        -- MAX(activation) used to return, so `total_activation` is unchanged;
        -- the depth tie-break only decides which of two equally-activated
        -- paths is reported, preferring the shorter. seed_id is the LAST key:
        -- it changes neither activation nor depth, only makes the reported
        -- seed of an exact tie deterministic.
        ranked AS (
            SELECT act.id, act.node_type, act.activation, act.depth, act.seed_id{ranked_parent},
                   ROW_NUMBER() OVER (
                       PARTITION BY act.id, act.node_type
                       ORDER BY act.activation DESC, act.depth ASC, act.seed_id ASC
                   ) AS rn
            FROM activation act
            {where_clause}
        ){final_sql}"""
    elif aggregation in _COMBINE_SQL:
        aggregate_sql = f"""
        -- F105 Task 7: per_seed keeps each seed's best path to a node, so
        -- parallel edges and cycles from one seed count once. ranked combines
        -- the seeds' contributions over the node and keeps the largest
        -- contributor's row (ordered as in max mode) for depth, seed_id and
        -- parent_id.
        per_seed AS (
            SELECT act.id, act.node_type, act.activation, act.depth, act.seed_id{ranked_parent},
                   ROW_NUMBER() OVER (
                       PARTITION BY act.id, act.node_type, act.seed_id
                       ORDER BY act.activation DESC, act.depth ASC
                   ) AS seed_rn
            FROM activation act
            {where_clause}
        ),
        ranked AS (
            SELECT act.id, act.node_type, act.depth, act.seed_id{ranked_parent},
                   {_COMBINE_SQL[aggregation]} AS total_activation,
                   ROW_NUMBER() OVER (
                       PARTITION BY act.id, act.node_type
                       ORDER BY act.activation DESC, act.depth ASC, act.seed_id ASC
                   ) AS rn
            FROM per_seed act
            WHERE act.seed_rn = 1
        ){final_sql}"""
    else:
        raise ValueError(f"unknown spreading aggregation {aggregation!r}")

    sql = text(f"""
        WITH RECURSIVE activation AS (
            SELECT id, node_type, score AS activation, 0 AS depth, id AS seed_id{seed_parent}
            FROM (VALUES {values_clause}) AS seeds(id, node_type, score)

            UNION ALL

            SELECT
                CASE WHEN e.source_id = a.id THEN e.target_id ELSE e.source_id END,
                CASE WHEN e.source_id = a.id THEN e.target_type ELSE e.source_type END,
                -- Clamp the weight term at 1.0 (no DB CHECK on
                -- graph_edges.weight). CASE, not LEAST: the suite's default
                -- SQLite backend has no LEAST() (codex PR #558 P2).
                a.activation
                    * CASE WHEN COALESCE(e.weight, 1.0) > 1.0 THEN 1.0
                           ELSE COALESCE(e.weight, 1.0) END
                    * :decay{fan_mult}{assoc_mult},
                a.depth + 1,
                a.seed_id{step_parent}
            FROM activation a{fan_join}
            JOIN brain.graph_edges e
                ON (e.source_id = a.id OR e.target_id = a.id)
            WHERE a.depth < :max_depth
                -- 2b: exclude non-associative edges (lineage supersedes,
                -- negative contradicts, temporal happened_before, builder
                -- co_occurred/co_mention) so spreading never traverses them.
                -- Single source of truth: graph_constants.autobehavior_exclusion_sql.
                AND {excl_e}
                AND e.agent_id = :agent_id
        ),{aggregate_sql}
    """)

    result = await session.execute(sql, params)
    rows = result.all()
    if rescore:
        if rescore_out is not None:
            for row in rows:
                if row.is_rescore:
                    rescore_out[row.id] = float(row.total_activation)
        # Sorted here too, so the order holds for a session that ignores ORDER BY.
        rows = sorted((r for r in rows if not r.is_rescore), key=lambda r: r.total_activation, reverse=True)
    if seed_out is not None:
        for row in rows:
            seed_out[row.id] = row.seed_id
    if parent_out is not None:
        for row in rows:
            parent_out[row.id] = row.parent_id
    return [
        (row.id, row.node_type, float(row.total_activation), int(row.depth))
        for row in rows
    ]


# F105 Task 5: where each graph node type keeps its embedding. Every table is
# agent-scoped by ``agent_id`` and holds a nullable ``vector(1536)``.
_EMBEDDING_TABLES: dict[str, str] = {
    "decision": "brain.decisions",
    "fact": "heart.facts",
    "episode": "heart.episodes",
    "procedure": "heart.procedures",
    "chunk": "heart.episode_chunks",
}


async def query_cosines(
    session: AsyncSession,
    agent_id: str,
    nodes: list[tuple[UUID, str]],
    query_vec: Sequence[float],
) -> dict[UUID, float | None]:
    """F105: ``cos(node, query) = 1 - (embedding <=> query)`` for each node.

    One query per node type present, ``id = ANY(:ids)``, this agent's rows only.
    A node is ``None`` when it has no embedding, no row (or another agent's
    row), or a type with no embedding table: absence is not a relevance value.
    """
    out: dict[UUID, float | None] = {nid: None for nid, _t in nodes}
    by_type: dict[str, list[str]] = {}
    for nid, ntype in nodes:
        if ntype in _EMBEDDING_TABLES:
            by_type.setdefault(ntype, []).append(str(nid))
    if not by_type:
        return out
    vec_lit = "[" + ",".join(f"{v:.6f}" for v in query_vec) + "]"
    for ntype, ids in by_type.items():
        result = await session.execute(text(f"""
            SELECT id, 1 - (embedding <=> CAST(:v AS vector)) AS cos
            FROM {_EMBEDDING_TABLES[ntype]}
            WHERE agent_id = :agent_id AND id = ANY(CAST(:ids AS uuid[])) AND embedding IS NOT NULL
        """), {"v": vec_lit, "agent_id": agent_id, "ids": ids})
        for row in result.all():
            if row.id in out:
                out[row.id] = float(row.cos)
    return out


def gate_factor(
    cos_node: float | None,
    cos_seed: float | None,
    mode: str,
    q_lo: float,
    q_hi: float,
    s_min: float,
) -> float:
    """F105: the query gate's multiplier for one spreading row, always in [0, 1].

    - ``seed_relative``: ``min(1, max(cos_node, 0) / max(cos_seed, s_min))``. A
      node as relevant to the query as the seed that activated it keeps full
      activation; a seed with a cosine below ``s_min`` (zero or negative too)
      is divided by ``s_min``, so it still gates. Neutral (1.0) when either
      cosine is None.
    - ``quantile``: ``clamp((cos_node - q_lo) / (q_hi - q_lo), 0, 1)``.
    - ``raw``: ``max(cos_node, 0)``.

    ``quantile`` and ``raw`` are neutral when ``cos_node`` is None. The result is
    clamped, so a cosine a hair above 1 cannot amplify.
    """
    if mode == "seed_relative":
        if cos_node is None or cos_seed is None:
            return 1.0
        factor = max(cos_node, 0.0) / max(cos_seed, s_min)
    elif mode == "quantile":
        if cos_node is None:
            return 1.0
        factor = (cos_node - q_lo) / (q_hi - q_lo)
    elif mode == "raw":
        if cos_node is None:
            return 1.0
        factor = cos_node
    else:
        raise ValueError(f"unknown spreading query gate mode {mode!r}")
    return min(1.0, max(0.0, factor))

