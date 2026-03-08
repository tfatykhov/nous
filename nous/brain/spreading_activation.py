"""F022 Phase 4 — Spreading activation with density gate.

Provides multi-hop graph traversal using a recursive CTE when
graph density exceeds a configurable threshold.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings

logger = logging.getLogger(__name__)


async def compute_graph_density(session: AsyncSession, agent_id: str) -> float:
    """Compute average edges per unique node for the given agent."""
    sql = text("""
        WITH node_counts AS (
            SELECT COUNT(*) AS edge_count,
                   (SELECT COUNT(DISTINCT node_id) FROM (
                       SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                       UNION
                       SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                   ) nodes) AS unique_nodes
            FROM brain.graph_edges
            WHERE agent_id = :agent_id
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
) -> list[tuple[UUID, str, float]]:
    """Run spreading activation CTE and return activated nodes.

    Args:
        seed_nodes: List of (node_id, node_type, score) from vector search

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
    }
    for i, (nid, ntype, score) in enumerate(seed_nodes):
        values_parts.append(f"(CAST(:id_{i} AS UUID), CAST(:type_{i} AS VARCHAR), CAST(:score_{i} AS FLOAT))")
        params[f"id_{i}"] = str(nid)
        params[f"type_{i}"] = ntype
        params[f"score_{i}"] = float(score)

    values_clause = ", ".join(values_parts)

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
                AND e.relation != 'contradicts'
                AND e.agent_id = :agent_id
        )
        SELECT id, node_type, SUM(activation) AS total_activation
        FROM activation
        GROUP BY id, node_type
        ORDER BY total_activation DESC
        LIMIT 20
    """)

    result = await session.execute(sql, params)
    return [(row.id, row.node_type, float(row.total_activation)) for row in result.all()]
