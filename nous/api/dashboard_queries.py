"""Dashboard query functions for F021 Memory Dashboard.

Each function takes (session, agent_id) and returns a dict suitable
for JSON serialisation.  They never manage sessions themselves.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.spreading_activation import compute_graph_density

logger = logging.getLogger(__name__)


# ── Task 5: Dashboard stats (used by GET /status?dashboard=true) ─────────


async def get_dashboard_stats(session: AsyncSession, agent_id: str) -> dict:
    """Return dashboard-level aggregates: deltas, distributions, timeseries, density."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # ── 7-day deltas ──
    deltas: dict[str, dict] = {}
    tables = [
        ("decisions", "brain.decisions", "created_at"),
        ("facts", "heart.facts", "created_at"),
        ("episodes", "heart.episodes", "created_at"),
        ("procedures", "heart.procedures", "created_at"),
    ]
    for key, table, ts_col in tables:
        result = await session.execute(
            text(f"""
                SELECT
                    COUNT(*) FILTER (WHERE {ts_col} >= :since) AS recent,
                    COUNT(*) AS total
                FROM {table}
                WHERE agent_id = :agent_id
            """),
            {"agent_id": agent_id, "since": seven_days_ago},
        )
        row = result.one()
        deltas[key] = {"total": row.total, "last_7_days": row.recent}

    # ── Distributions ──
    # Fact categories
    result = await session.execute(
        text("""
            SELECT COALESCE(category, 'uncategorized') AS cat, COUNT(*) AS cnt
            FROM heart.facts
            WHERE agent_id = :agent_id AND active = true
            GROUP BY cat ORDER BY cnt DESC
        """),
        {"agent_id": agent_id},
    )
    fact_categories = {row.cat: row.cnt for row in result}

    # Decision outcomes
    result = await session.execute(
        text("""
            SELECT COALESCE(outcome, 'pending') AS out, COUNT(*) AS cnt
            FROM brain.decisions
            WHERE agent_id = :agent_id
            GROUP BY out ORDER BY cnt DESC
        """),
        {"agent_id": agent_id},
    )
    decision_outcomes = {row.out: row.cnt for row in result}

    # Decision categories
    result = await session.execute(
        text("""
            SELECT category, COUNT(*) AS cnt
            FROM brain.decisions
            WHERE agent_id = :agent_id
            GROUP BY category ORDER BY cnt DESC
        """),
        {"agent_id": agent_id},
    )
    decision_categories = {row.category: row.cnt for row in result}

    # Edge relation distribution
    result = await session.execute(
        text("""
            SELECT relation, COUNT(*) AS cnt
            FROM brain.graph_edges
            WHERE agent_id = :agent_id
            GROUP BY relation ORDER BY cnt DESC
        """),
        {"agent_id": agent_id},
    )
    edge_relations = {row.relation: row.cnt for row in result}

    # ── Timeseries: daily counts for 30 days ──
    timeseries: dict[str, list[dict]] = {}
    for key, table, ts_col in tables:
        result = await session.execute(
            text(f"""
                SELECT d::date AS day, COUNT(t.{ts_col}) AS cnt
                FROM generate_series(:since::date, :now::date, '1 day') AS d
                LEFT JOIN {table} t
                    ON t.{ts_col}::date = d::date AND t.agent_id = :agent_id
                GROUP BY day ORDER BY day
            """),
            {"agent_id": agent_id, "since": thirty_days_ago, "now": now},
        )
        timeseries[key] = [
            {"date": row.day.isoformat(), "count": row.cnt} for row in result
        ]

    # ── Graph density ──
    density = await compute_graph_density(session, agent_id)

    return {
        "deltas": deltas,
        "distributions": {
            "fact_categories": fact_categories,
            "decision_outcomes": decision_outcomes,
            "decision_categories": decision_categories,
            "edge_relations": edge_relations,
        },
        "timeseries": timeseries,
        "graph_density": density,
    }


# ── Task 6: Graph data (GET /dashboard/graph) ───────────────────────────


async def get_graph_data(
    session: AsyncSession, agent_id: str, *, limit: int = 200
) -> dict:
    """Return nodes + edges for D3 graph visualization."""
    max_edges = limit * 4

    # Fetch edges (limited)
    result = await session.execute(
        text("""
            SELECT id, source_id, target_id, source_type, target_type,
                   relation, weight, auto_linked, created_at
            FROM brain.graph_edges
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT :max_edges
        """),
        {"agent_id": agent_id, "max_edges": max_edges},
    )
    edges_raw = result.fetchall()

    edges = []
    node_ids: set[str] = set()
    for e in edges_raw:
        src = str(e.source_id)
        tgt = str(e.target_id)
        node_ids.add(src)
        node_ids.add(tgt)
        edges.append({
            "id": str(e.id),
            "source": src,
            "target": tgt,
            "source_type": e.source_type,
            "target_type": e.target_type,
            "relation": e.relation,
            "weight": e.weight,
            "auto_linked": e.auto_linked,
        })

    # Build nodes from source tables
    nodes: list[dict] = []
    type_queries = {
        "decision": (
            "brain.decisions",
            "LEFT(description, 120) AS label, category",
        ),
        "fact": (
            "heart.facts",
            "LEFT(content, 120) AS label, category",
        ),
        "episode": (
            "heart.episodes",
            "LEFT(COALESCE(title, summary), 120) AS label, frame_used AS category",
        ),
        "procedure": (
            "heart.procedures",
            "LEFT(name, 120) AS label, domain AS category",
        ),
    }

    for node_type, (table, cols) in type_queries.items():
        type_ids = list(node_ids)
        if not type_ids:
            continue
        result = await session.execute(
            text(f"""
                SELECT id::text, {cols}, created_at
                FROM {table}
                WHERE agent_id = :agent_id AND id = ANY(:ids)
            """),
            {"agent_id": agent_id, "ids": type_ids},
        )
        for row in result:
            if row.id in node_ids:
                nodes.append({
                    "id": row.id,
                    "type": node_type,
                    "label": row.label or "",
                    "category": row.category,
                })

    # Orphan counts (nodes with no edges at all — query source tables)
    orphan_counts: dict[str, int] = {}
    orphan_queries = [
        ("decisions", "brain.decisions"),
        ("facts", "heart.facts"),
        ("episodes", "heart.episodes"),
        ("procedures", "heart.procedures"),
    ]
    for key, table in orphan_queries:
        result = await session.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM {table} t
                WHERE t.agent_id = :agent_id
                  AND NOT EXISTS (
                      SELECT 1 FROM brain.graph_edges e
                      WHERE e.agent_id = :agent_id
                        AND (e.source_id = t.id OR e.target_id = t.id)
                  )
            """),
            {"agent_id": agent_id},
        )
        orphan_counts[key] = result.scalar() or 0

    # Total edge count
    result = await session.execute(
        text("SELECT COUNT(*) FROM brain.graph_edges WHERE agent_id = :agent_id"),
        {"agent_id": agent_id},
    )
    total_edges = result.scalar() or 0

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_edges": total_edges,
            "displayed_edges": len(edges),
            "node_count": len(nodes),
            "orphan_counts": orphan_counts,
        },
    }
