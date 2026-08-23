"""F022 Phase 4 — Spreading activation with density gate.

Provides multi-hop graph traversal using a recursive CTE when
graph density exceeds a configurable threshold.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.graph_constants import autobehavior_exclusion_sql
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


async def spreading_activation_search(
    session: AsyncSession,
    agent_id: str,
    seed_nodes: list[tuple[UUID, str, float]],
    settings: Settings,
    limit: int = 20,
    exclude_ids: set[UUID] | None = None,
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
        enforced here rather than assumed of every writer.

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
    conditions: list[str] = []
    if exclude_ids:
        params["excluded_ids"] = [str(x) for x in exclude_ids]
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

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    for i, (nid, ntype, score) in enumerate(seed_nodes):
        values_parts.append(f"(CAST(:id_{i} AS UUID), CAST(:type_{i} AS VARCHAR), CAST(:score_{i} AS FLOAT))")
        params[f"id_{i}"] = str(nid)
        params[f"type_{i}"] = ntype
        params[f"score_{i}"] = float(score)

    values_clause = ", ".join(values_parts)
    # 2b: same auto-behavior exclusion as the density gate (graph_constants).
    excl_e = autobehavior_exclusion_sql("e.")

    sql = text(f"""
        WITH RECURSIVE activation AS (
            SELECT id, node_type, score AS activation, 0 AS depth
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
                    * :decay,
                a.depth + 1
            FROM activation a
            JOIN brain.graph_edges e
                ON (e.source_id = a.id OR e.target_id = a.id)
            WHERE a.depth < :max_depth
                -- 2b: exclude non-associative edges (lineage supersedes,
                -- negative contradicts, temporal happened_before, builder
                -- co_occurred/co_mention) so spreading never traverses them.
                -- Single source of truth: graph_constants.autobehavior_exclusion_sql.
                AND {excl_e}
                AND e.agent_id = :agent_id
        ),
        -- A8: keep the winning path's depth alongside its activation.
        -- ROW_NUMBER over (activation DESC, depth ASC) selects exactly the row
        -- MAX(activation) used to return, so `total_activation` is unchanged;
        -- the depth tie-break only decides which of two equally-activated
        -- paths is reported, preferring the shorter.
        ranked AS (
            SELECT act.id, act.node_type, act.activation, act.depth,
                   ROW_NUMBER() OVER (
                       PARTITION BY act.id, act.node_type
                       ORDER BY act.activation DESC, act.depth ASC
                   ) AS rn
            FROM activation act
            {where_clause}
        )
        SELECT id, node_type, activation AS total_activation, depth
        FROM ranked
        WHERE rn = 1
        ORDER BY total_activation DESC
        LIMIT :result_limit
    """)

    result = await session.execute(sql, params)
    return [
        (row.id, row.node_type, float(row.total_activation), int(row.depth))
        for row in result.all()
    ]
