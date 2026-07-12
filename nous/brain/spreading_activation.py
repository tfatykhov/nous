"""F022 Phase 4 — Spreading activation with density gate.

Provides multi-hop graph traversal using a recursive CTE when
graph density exceeds a configurable threshold.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.graph_constants import (
    AUTOBEHAVIOR_EXCLUDED_RELATIONS,
    autobehavior_exclusion_sql,
)
from nous.config import Settings

logger = logging.getLogger(__name__)


async def compute_graph_density(session: AsyncSession, agent_id: str) -> float:
    """Compute average edges per unique node for the given agent.

    F076: co-mention edges (``extraction_method='co_mention'``) are EXCLUDED from
    the density count. The co-mention builder is default-on but its retrieval
    consumers (Path A / adjacency / seed-score) default off, so its edges must not
    silently push an agent over ``spreading_activation_density_threshold`` and flip
    decision retrieval into spreading activation before that rollout is intentional.
    ``IS DISTINCT FROM`` keeps NULL/legacy ``extraction_method`` rows counted.

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
) -> list[tuple[UUID, str, float]]:
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
        List of (node_id, node_type, total_activation) sorted by activation desc
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
    exclude_clause = ""
    if exclude_ids:
        params["excluded_ids"] = [str(x) for x in exclude_ids]
        exclude_clause = "WHERE id != ALL(CAST(:excluded_ids AS uuid[]))"
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
                a.activation * COALESCE(e.weight, 1.0) * :decay,
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
        )
        SELECT id, node_type, SUM(activation) AS total_activation
        FROM activation
        {exclude_clause}
        GROUP BY id, node_type
        ORDER BY total_activation DESC
        LIMIT :result_limit
    """)

    result = await session.execute(sql, params)
    return [(row.id, row.node_type, float(row.total_activation)) for row in result.all()]
