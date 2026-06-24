"""Dashboard query functions for F021 Memory Dashboard.

Each function takes (session, agent_id) and returns a dict suitable
for JSON serialisation.  They never manage sessions themselves.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from collections import Counter, defaultdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.spreading_activation import compute_graph_density

logger = logging.getLogger(__name__)


# ── Soft-delete-aware orphan counting ────────────────────────────────────
#
# facts/episodes/procedures soft-delete via an `active` boolean; decisions and
# episode_chunks have no such column. Orphan metrics must exclude soft-deleted
# nodes so the orphan rate reflects the LIVE graph — retired raw episode
# transcripts and pruned reflection facts are orphans by design (the backfill
# only links active nodes), not coverage gaps. Single source of truth so the
# three orphan call-sites (graph / health / density) can never drift apart.
_ACTIVE_FILTER_TABLES = {"heart.facts", "heart.episodes", "heart.procedures"}


def _active_clause(table: str, alias: str = "t") -> str:
    """Return ` AND <alias>.active = true` for soft-deletable tables, else ''."""
    return f" AND {alias}.active = true" if table in _ACTIVE_FILTER_TABLES else ""


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
                SELECT CAST(d AS date) AS day, COUNT(t.{ts_col}) AS cnt
                FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
                LEFT JOIN {table} t
                    ON CAST(t.{ts_col} AS date) = CAST(d AS date) AND t.agent_id = :agent_id
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
                   relation, weight, auto_linked, extraction_method, created_at
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
            # F065: deterministic / heuristic / inferred provenance.
            "extraction_method": e.extraction_method,
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
        # F067: chunked raw transcripts. No category column on the table —
        # surface chunk_index instead so the detail panel has something
        # meaningful to show, but cast to text to keep the column shape
        # uniform across types.
        "chunk": (
            "heart.episode_chunks",
            "LEFT(content, 120) AS label, chunk_index::text AS category",
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
        # F067: chunks are orphaned when no F070 part_of/summarized_by/related_to
        # backfill has linked them yet.
        ("chunks", "heart.episode_chunks"),
    ]
    for key, table in orphan_queries:
        result = await session.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM {table} t
                WHERE t.agent_id = :agent_id{_active_clause(table)}
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


# ── Node detail (GET /dashboard/graph/node/{node_id}) ────────────────────
#
# Per-type source table + full-content/category expressions. Reused to hydrate
# a single node (untruncated) and to label its neighbors (LEFT(.,120)).
_NODE_DETAIL_SOURCES: dict[str, tuple[str, str, str]] = {
    "decision": ("brain.decisions", "description", "category"),
    "fact": ("heart.facts", "content", "category"),
    "episode": ("heart.episodes", "COALESCE(title, summary)", "frame_used"),
    "procedure": ("heart.procedures", "name", "domain"),
    "chunk": ("heart.episode_chunks", "content", "chunk_index::text"),
}

# Cap connections returned for a single node so a dense hub can't produce a
# multi-MB payload. The strongest-weight edges are kept (ORDER BY weight DESC).
_NODE_DETAIL_EDGE_LIMIT = 200


async def get_node_detail(
    session: AsyncSession, agent_id: str, node_id: str, node_type: str
) -> dict:
    """Return one graph node's full content + ALL its connections.

    Unlike ``brain.neighbors()`` (retrieval-flavored — it hides ``supersedes``,
    ``contradicts``, and inactive neighbors), this surfaces every edge in both
    directions so a human inspecting the graph can see lineage and conflicts.

    Returns ``{found: False}`` when the node id/type is unknown (e.g. a
    hard-deleted node still referenced by a stale edge), else
    ``{found: True, node, connections, connection_count}``.
    """
    src = _NODE_DETAIL_SOURCES.get(node_type)
    if src is None:
        return {"found": False}
    table, content_expr, cat_expr = src

    # Hydrate the node itself with full, untruncated content.
    row = (
        await session.execute(
            text(f"""
                SELECT id::text AS id, {content_expr} AS content,
                       ({cat_expr})::text AS category, created_at
                FROM {table}
                WHERE agent_id = :agent_id AND id = CAST(:node_id AS uuid)
            """),
            {"agent_id": agent_id, "node_id": node_id},
        )
    ).first()
    if row is None:
        return {"found": False}

    node = {
        "id": row.id,
        "type": node_type,
        "content": row.content or "",
        "category": row.category,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }

    # Every edge touching this node, either direction, strongest first. Fetch
    # one over the cap to detect (and flag) truncation on dense hub nodes.
    edges = (
        await session.execute(
            text("""
                SELECT id::text AS edge_id,
                       source_id::text AS source_id, target_id::text AS target_id,
                       source_type, target_type, relation, weight,
                       extraction_method, auto_linked
                FROM brain.graph_edges
                WHERE agent_id = :agent_id
                  AND (source_id = CAST(:node_id AS uuid)
                       OR target_id = CAST(:node_id AS uuid))
                ORDER BY weight DESC NULLS LAST, relation
                LIMIT :edge_limit
            """),
            {"agent_id": agent_id, "node_id": node_id,
             "edge_limit": _NODE_DETAIL_EDGE_LIMIT + 1},
        )
    ).fetchall()
    truncated = len(edges) > _NODE_DETAIL_EDGE_LIMIT
    edges = edges[:_NODE_DETAIL_EDGE_LIMIT]

    nid = node["id"]
    connections: list[dict] = []
    neighbor_ids_by_type: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e.source_id == nid:
            direction, neighbor_id, neighbor_type = "out", e.target_id, e.target_type
        else:
            direction, neighbor_id, neighbor_type = "in", e.source_id, e.source_type
        connections.append({
            "edge_id": e.edge_id,
            "neighbor_id": neighbor_id,
            "neighbor_type": neighbor_type,
            "relation": e.relation,
            "direction": direction,
            "weight": e.weight,
            "extraction_method": e.extraction_method,
            "auto_linked": e.auto_linked,
        })
        if neighbor_type in _NODE_DETAIL_SOURCES:
            neighbor_ids_by_type[neighbor_type].add(neighbor_id)

    # Hydrate neighbor labels (truncated) + active flag, one query per type.
    labels: dict[str, str] = {}
    active_flags: dict[str, bool] = {}
    for ntype, ids in neighbor_ids_by_type.items():
        table_n, content_expr_n, _ = _NODE_DETAIL_SOURCES[ntype]
        active_sel = (
            "active" if table_n in _ACTIVE_FILTER_TABLES else "true AS active"
        )
        res = await session.execute(
            text(f"""
                SELECT id::text AS id, LEFT({content_expr_n}, 120) AS label, {active_sel}
                FROM {table_n}
                WHERE agent_id = :agent_id AND id = ANY(CAST(:ids AS uuid[]))
            """),
            {"agent_id": agent_id, "ids": list(ids)},
        )
        for r in res:
            labels[r.id] = r.label or ""
            active_flags[r.id] = bool(r.active)

    for c in connections:
        c["neighbor_label"] = labels.get(c["neighbor_id"], "")
        # A neighbor missing from the label lookup is hard-deleted/dangling.
        c["neighbor_active"] = active_flags.get(c["neighbor_id"], False)

    return {
        "found": True,
        "node": node,
        "connections": connections,
        "connection_count": len(connections),
        "connections_truncated": truncated,
    }


# ── Task 7: Calibration data (GET /dashboard/calibration) ───────────────


async def get_calibration_data(session: AsyncSession, agent_id: str) -> dict:
    """Return calibration curve, histograms, and history for dashboard."""

    # Calibration curve: bucket confidence into 10 bins, compute accuracy per bin
    result = await session.execute(
        text("""
            SELECT
                FLOOR(confidence * 10) / 10 AS bin,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE outcome = 'success') AS successes,
                AVG(confidence) AS avg_confidence
            FROM brain.decisions
            WHERE agent_id = :agent_id AND outcome IS NOT NULL AND outcome != 'pending'
            GROUP BY bin ORDER BY bin
        """),
        {"agent_id": agent_id},
    )
    calibration_curve = [
        {
            "bucket": f"{float(row.bin):.1f}",
            "actual_success_rate": row.successes / row.total if row.total > 0 else 0.0,
            "total": row.total,
            "successes": row.successes,
            "avg_confidence": float(row.avg_confidence) if row.avg_confidence else 0.0,
        }
        for row in result
    ]

    # Confidence histogram (all decisions)
    result = await session.execute(
        text("""
            SELECT
                FLOOR(confidence * 10) / 10 AS bin,
                COUNT(*) AS cnt
            FROM brain.decisions
            WHERE agent_id = :agent_id
            GROUP BY bin ORDER BY bin
        """),
        {"agent_id": agent_id},
    )
    confidence_histogram = [
        {"range": f"{float(row.bin):.1f}-{float(row.bin) + 0.1:.1f}", "count": row.cnt}
        for row in result
    ]

    # Outcome by category
    result = await session.execute(
        text("""
            SELECT category, outcome, COUNT(*) AS cnt
            FROM brain.decisions
            WHERE agent_id = :agent_id AND outcome IS NOT NULL AND outcome != 'pending'
            GROUP BY category, outcome
            ORDER BY category, outcome
        """),
        {"agent_id": agent_id},
    )
    outcome_by_category: dict[str, dict[str, int]] = {}
    for row in result:
        outcome_by_category.setdefault(row.category, {})[row.outcome] = row.cnt

    # Outcome by stakes
    result = await session.execute(
        text("""
            SELECT stakes, outcome, COUNT(*) AS cnt
            FROM brain.decisions
            WHERE agent_id = :agent_id AND outcome IS NOT NULL AND outcome != 'pending'
            GROUP BY stakes, outcome
            ORDER BY stakes, outcome
        """),
        {"agent_id": agent_id},
    )
    outcome_by_stakes: dict[str, dict[str, int]] = {}
    for row in result:
        outcome_by_stakes.setdefault(row.stakes, {})[row.outcome] = row.cnt

    # Reason type stats
    result = await session.execute(
        text("""
            SELECT r.type, COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE d.outcome = 'success') AS successes,
                   COUNT(*) FILTER (WHERE d.outcome IS NOT NULL AND d.outcome != 'pending') AS reviewed
            FROM brain.decision_reasons r
            JOIN brain.decisions d ON d.id = r.decision_id AND d.agent_id = :agent_id
            GROUP BY r.type ORDER BY cnt DESC
        """),
        {"agent_id": agent_id},
    )
    reason_stats = {
        row.type: {
            "count": row.cnt,
            "success_rate": row.successes / row.reviewed if row.reviewed > 0 else 0.0,
            "successes": row.successes,
            "reviewed": row.reviewed,
        }
        for row in result
    }

    # Brier score over time: compute running Brier from reviewed decisions
    # Brier score = mean of (confidence - outcome)^2 where outcome is 1 for success, 0 otherwise
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)
    result = await session.execute(
        text("""
            WITH reviewed AS (
                SELECT
                    CAST(created_at AS date) AS day,
                    confidence,
                    CASE WHEN outcome = 'success' THEN 1.0 ELSE 0.0 END AS outcome_val
                FROM brain.decisions
                WHERE agent_id = :agent_id
                  AND outcome IS NOT NULL AND outcome != 'pending'
                  AND created_at >= :since
                ORDER BY created_at
            ),
            daily_brier AS (
                SELECT day,
                       AVG(POWER(confidence - outcome_val, 2)) AS brier_score,
                       COUNT(*) AS cnt
                FROM reviewed
                GROUP BY day
            )
            SELECT CAST(d AS date) AS day,
                   daily_brier.brier_score,
                   daily_brier.cnt
            FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
            LEFT JOIN daily_brier ON daily_brier.day = CAST(d AS date)
            ORDER BY day
        """),
        {"agent_id": agent_id, "since": thirty_days_ago, "now": now},
    )
    # Only include days that had reviewed decisions
    brier_history = [
        {
            "brier_score": round(float(row.brier_score), 4),
            "date": row.day.isoformat(),
        }
        for row in result
        if row.brier_score is not None
    ]

    # Daily decisions (last 30 days) — reuses now/thirty_days_ago from above
    result = await session.execute(
        text("""
            SELECT CAST(d AS date) AS day,
                   COUNT(t.created_at) AS cnt,
                   COUNT(t.created_at) FILTER (WHERE t.outcome = 'success') AS successes,
                   COUNT(t.created_at) FILTER (WHERE t.outcome IS NOT NULL AND t.outcome != 'pending') AS reviewed
            FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
            LEFT JOIN brain.decisions t
                ON CAST(t.created_at AS date) = CAST(d AS date) AND t.agent_id = :agent_id
            GROUP BY day ORDER BY day
        """),
        {"agent_id": agent_id, "since": thirty_days_ago, "now": now},
    )
    daily_decisions = [
        {
            "date": row.day.isoformat(),
            "count": row.cnt,
            "successes": row.successes,
            "reviewed": row.reviewed,
        }
        for row in result
    ]

    return {
        "calibration_curve": calibration_curve,
        "confidence_histogram": confidence_histogram,
        "outcome_by_category": outcome_by_category,
        "outcome_by_stakes": outcome_by_stakes,
        "reason_type_stats": reason_stats,
        "brier_history": brier_history,
        "daily_decisions": daily_decisions,
    }


# ── Task 8: Activity data (GET /dashboard/activity) ─────────────────────


async def get_activity_data(session: AsyncSession, agent_id: str, hours: int = 168) -> dict:
    """Return activity events + censor/schedule/sleep stats for the dashboard."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    seven_days_ago = now - timedelta(days=7)

    # Individual event rows
    result = await session.execute(
        text("""
            SELECT event_type AS type, created_at, data
            FROM nous_system.events
            WHERE agent_id = :agent_id AND created_at >= :since
            ORDER BY created_at DESC LIMIT 100
        """),
        {"agent_id": agent_id, "since": since},
    )
    events = []
    for row in result:
        evt: dict = {"type": row.type, "created_at": row.created_at.isoformat(), "data": row.data}
        events.append(evt)

    # Censor base stats
    result = await session.execute(
        text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE active = true) AS active,
                   COUNT(*) FILTER (WHERE created_by = 'manual') AS manual_created,
                   COUNT(*) FILTER (WHERE created_by != 'manual') AS auto_created
            FROM heart.censors WHERE agent_id = :agent_id
        """),
        {"agent_id": agent_id},
    )
    censor_row = result.one()

    # Censor 7d activations from events (not cumulative counters)
    result = await session.execute(
        text("""
            SELECT COUNT(*) AS cnt FROM nous_system.events
            WHERE agent_id = :agent_id AND event_type = 'censor_triggered'
              AND created_at >= :since
        """),
        {"agent_id": agent_id, "since": seven_days_ago},
    )
    total_activations_7d = result.scalar() or 0

    # False positives from events (event type may not exist yet)
    result = await session.execute(
        text("""
            SELECT COUNT(*) AS cnt FROM nous_system.events
            WHERE agent_id = :agent_id AND event_type = 'censor_false_positive'
              AND created_at >= :since
        """),
        {"agent_id": agent_id, "since": seven_days_ago},
    )
    false_positives_7d = result.scalar() or 0

    # Top 5 censors by activation_count
    result = await session.execute(
        text("""
            SELECT id::text, trigger_pattern, activation_count AS activations
            FROM heart.censors
            WHERE agent_id = :agent_id AND activation_count > 0
            ORDER BY activation_count DESC LIMIT 5
        """),
        {"agent_id": agent_id},
    )
    top_censors = [{"id": row.id, "trigger_pattern": row.trigger_pattern,
                    "activations": row.activations} for row in result]

    censor_stats = {
        "total": censor_row.total,
        "active": censor_row.active,
        "auto_created": censor_row.auto_created,
        "manual_created": censor_row.manual_created,
        "total_activations_7d": total_activations_7d,
        "false_positives_7d": false_positives_7d,
        "top_censors": top_censors,
    }

    # Schedule base stats
    result = await session.execute(
        text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE active = true) AS active
            FROM heart.schedules WHERE agent_id = :agent_id
        """),
        {"agent_id": agent_id},
    )
    sched_row = result.one()

    # Schedule fires in 7d from events
    result = await session.execute(
        text("""
            SELECT COUNT(*) FROM nous_system.events
            WHERE agent_id = :agent_id AND event_type = 'schedule_fired'
              AND created_at >= :since
        """),
        {"agent_id": agent_id, "since": seven_days_ago},
    )
    fires_7d = result.scalar() or 0

    # Next upcoming fires
    result = await session.execute(
        text("""
            SELECT id::text, task, next_fire_at
            FROM heart.schedules
            WHERE agent_id = :agent_id AND active = true AND next_fire_at IS NOT NULL
            ORDER BY next_fire_at LIMIT 5
        """),
        {"agent_id": agent_id},
    )
    next_fires = [{"id": row.id, "task": row.task,
                   "next_fire_at": row.next_fire_at.isoformat()} for row in result]

    schedule_stats = {
        "total": sched_row.total,
        "active": sched_row.active,
        "fires_7d": fires_7d,
        "next_fires": next_fires,
    }

    # Sleep stats
    result = await session.execute(
        text("""
            SELECT COUNT(*) AS total_sleeps, MAX(created_at) AS last_sleep
            FROM nous_system.events
            WHERE agent_id = :agent_id AND event_type = 'sleep_started'
        """),
        {"agent_id": agent_id},
    )
    sleep_row = result.one()
    total_sleeps = sleep_row.total_sleeps or 0
    last_sleep = sleep_row.last_sleep

    # Last sleep_completed event data for facts/procedures/censors
    result = await session.execute(
        text("""
            SELECT data FROM nous_system.events
            WHERE agent_id = :agent_id AND event_type = 'sleep_completed'
            ORDER BY created_at DESC LIMIT 1
        """),
        {"agent_id": agent_id},
    )
    completed_row = result.first()
    completed_data = (completed_row.data if completed_row else None) or {}

    sleep_stats = {
        "total_sleeps": total_sleeps,
        "last_sleep": last_sleep.isoformat() if last_sleep else None,
        "facts_created": completed_data.get("facts_created", 0),
        "procedures_created": completed_data.get("procedures_created", 0),
        "censors_retired": completed_data.get("censors_retired", 0),
    }

    return {
        "events": events,
        "censor_stats": censor_stats,
        "schedule_stats": schedule_stats,
        "sleep_stats": sleep_stats,
    }


# ── Task 9: Health data (GET /dashboard/health) ─────────────────────────


async def get_health_data(session: AsyncSession, agent_id: str) -> dict:
    """Return graph health metrics: edge creation, degree distribution, density, orphans."""
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    # Daily edge creation with auto/manual split (last 30 days)
    result = await session.execute(
        text("""
            SELECT CAST(d AS date) AS day,
                   COUNT(e.created_at) AS cnt,
                   COUNT(e.created_at) FILTER (WHERE e.auto_linked = true) AS auto_cnt,
                   COUNT(e.created_at) FILTER (WHERE e.auto_linked = false OR e.auto_linked IS NULL) AS manual_cnt
            FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
            LEFT JOIN brain.graph_edges e
                ON CAST(e.created_at AS date) = CAST(d AS date) AND e.agent_id = :agent_id
            GROUP BY day ORDER BY day
        """),
        {"agent_id": agent_id, "since": thirty_days_ago, "now": now},
    )
    daily_edges = [
        {
            "date": row.day.isoformat(),
            "count": row.cnt,
            "auto": row.auto_cnt,
            "manual": row.manual_cnt,
        }
        for row in result
    ]

    # Degree distribution (how many nodes have degree 1, 2, 3, ...)
    result = await session.execute(
        text("""
            WITH node_degrees AS (
                SELECT node_id, COUNT(*) AS degree
                FROM (
                    SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                    UNION ALL
                    SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                ) all_nodes
                GROUP BY node_id
            )
            SELECT degree, COUNT(*) AS node_count
            FROM node_degrees
            GROUP BY degree ORDER BY degree
        """),
        {"agent_id": agent_id},
    )
    degree_distribution = [
        {"degree": row.degree, "count": row.node_count} for row in result
    ]

    # Graph density
    density = await compute_graph_density(session, agent_id)

    # Orphan counts per type
    orphan_counts: dict[str, int] = {}
    orphan_queries = [
        ("decisions", "brain.decisions"),
        ("facts", "heart.facts"),
        ("episodes", "heart.episodes"),
        ("procedures", "heart.procedures"),
        # F067: include chunked transcripts so the health card reflects
        # F070/F070.1 backfill coverage, not just the legacy 4 types.
        ("chunks", "heart.episode_chunks"),
    ]
    for key, table in orphan_queries:
        result = await session.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM {table} t
                WHERE t.agent_id = :agent_id{_active_clause(table)}
                  AND NOT EXISTS (
                      SELECT 1 FROM brain.graph_edges e
                      WHERE e.agent_id = :agent_id
                        AND (e.source_id = t.id OR e.target_id = t.id)
                  )
            """),
            {"agent_id": agent_id},
        )
        orphan_counts[key] = result.scalar() or 0

    total_orphans = sum(orphan_counts.values())

    # Total counts for context
    result = await session.execute(
        text("""
            SELECT
                (SELECT COUNT(*) FROM brain.graph_edges WHERE agent_id = :agent_id) AS total_edges,
                (SELECT COUNT(DISTINCT node_id) FROM (
                    SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                    UNION
                    SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                ) n) AS connected_nodes
        """),
        {"agent_id": agent_id},
    )
    totals = result.one()

    # Density history: daily cumulative density using window functions
    # Aggregates daily new edges/nodes, then running sum → density per day
    result = await session.execute(
        text("""
            WITH daily AS (
                SELECT CAST(d AS date) AS day
                FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
            ),
            daily_new_edges AS (
                SELECT CAST(created_at AS date) AS day, COUNT(*) AS cnt
                FROM brain.graph_edges
                WHERE agent_id = :agent_id AND created_at >= :since
                GROUP BY CAST(created_at AS date)
            ),
            daily_new_nodes AS (
                SELECT day, COUNT(DISTINCT node_id) AS cnt
                FROM (
                    SELECT CAST(created_at AS date) AS day, source_id AS node_id
                    FROM brain.graph_edges
                    WHERE agent_id = :agent_id
                    UNION
                    SELECT CAST(created_at AS date) AS day, target_id AS node_id
                    FROM brain.graph_edges
                    WHERE agent_id = :agent_id
                ) first_seen
                GROUP BY day
            ),
            pre_period AS (
                SELECT
                    COUNT(*) AS edge_base,
                    COUNT(DISTINCT node_id) AS node_base
                FROM (
                    SELECT id, source_id AS node_id FROM brain.graph_edges
                    WHERE agent_id = :agent_id AND created_at < :since
                    UNION ALL
                    SELECT id, target_id AS node_id FROM brain.graph_edges
                    WHERE agent_id = :agent_id AND created_at < :since
                ) pre
            ),
            joined AS (
                SELECT d.day,
                       COALESCE(e.cnt, 0) AS new_edges,
                       COALESCE(n.cnt, 0) AS new_nodes
                FROM daily d
                LEFT JOIN daily_new_edges e ON e.day = d.day
                LEFT JOIN daily_new_nodes n ON n.day = d.day
            )
            SELECT j.day,
                   (SELECT edge_base FROM pre_period)
                       + SUM(j.new_edges) OVER (ORDER BY j.day) AS cum_edges,
                   (SELECT node_base FROM pre_period)
                       + SUM(j.new_nodes) OVER (ORDER BY j.day) AS cum_nodes
            FROM joined j
            ORDER BY j.day
        """),
        {"agent_id": agent_id, "since": thirty_days_ago, "now": now},
    )
    density_history = [
        {
            "date": row.day.isoformat(),
            "density": round(int(row.cum_edges) / int(row.cum_nodes), 2)
            if row.cum_nodes and int(row.cum_nodes) > 0 else 0.0,
        }
        for row in result
    ]

    # Orphan trend: daily orphan count using window functions
    # Count new total nodes and new connected nodes per day, running sum, diff
    result = await session.execute(
        text("""
            WITH daily AS (
                SELECT CAST(d AS date) AS day
                FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
            ),
            all_nodes AS (
                SELECT id, created_at FROM brain.decisions WHERE agent_id = :agent_id
                UNION ALL
                SELECT id, created_at FROM heart.facts WHERE agent_id = :agent_id AND active = true
                UNION ALL
                SELECT id, created_at FROM heart.episodes WHERE agent_id = :agent_id AND active = true
                UNION ALL
                SELECT id, created_at FROM heart.procedures WHERE agent_id = :agent_id AND active = true
                UNION ALL
                -- F067: chunks must count in the daily orphan trend so the
                -- post-F070 backfill curve is visible.
                SELECT id, created_at FROM heart.episode_chunks WHERE agent_id = :agent_id
            ),
            daily_new_total AS (
                SELECT CAST(created_at AS date) AS day, COUNT(*) AS cnt
                FROM all_nodes
                GROUP BY CAST(created_at AS date)
            ),
            daily_new_connected AS (
                -- #381: count each node ONCE, on the date its FIRST edge appeared.
                -- The prior form counted a node on every day it had any edge, so a
                -- node edged on multiple days inflated the running connected sum past
                -- the running total and GREATEST(...,0) clamped orphan_count to 0.
                SELECT first_connected_day AS day, COUNT(*) AS cnt
                FROM (
                    SELECT node_id, CAST(MIN(created_at) AS date) AS first_connected_day
                    FROM (
                        SELECT source_id AS node_id, created_at
                        FROM brain.graph_edges WHERE agent_id = :agent_id
                        UNION ALL
                        SELECT target_id AS node_id, created_at
                        FROM brain.graph_edges WHERE agent_id = :agent_id
                    ) all_endpoints
                    GROUP BY node_id
                ) first_seen
                GROUP BY first_connected_day
            ),
            pre_period AS (
                SELECT
                    (SELECT COUNT(*) FROM all_nodes WHERE created_at < :since) AS total_base,
                    (SELECT COUNT(DISTINCT node_id) FROM (
                        SELECT source_id AS node_id FROM brain.graph_edges
                        WHERE agent_id = :agent_id AND created_at < :since
                        UNION
                        SELECT target_id AS node_id FROM brain.graph_edges
                        WHERE agent_id = :agent_id AND created_at < :since
                    ) n) AS connected_base
            ),
            joined AS (
                SELECT d.day,
                       COALESCE(t.cnt, 0) AS new_total,
                       COALESCE(c.cnt, 0) AS new_connected
                FROM daily d
                LEFT JOIN daily_new_total t ON t.day = d.day
                LEFT JOIN daily_new_connected c ON c.day = d.day
            )
            SELECT j.day,
                   GREATEST(
                       ((SELECT total_base FROM pre_period) + SUM(j.new_total) OVER (ORDER BY j.day))
                       - ((SELECT connected_base FROM pre_period) + SUM(j.new_connected) OVER (ORDER BY j.day)),
                       0
                   ) AS orphan_count
            FROM joined j
            ORDER BY j.day
        """),
        {"agent_id": agent_id, "since": thirty_days_ago, "now": now},
    )
    orphan_trend = [
        {"date": row.day.isoformat(), "count": int(row.orphan_count)}
        for row in result
    ]

    return {
        "daily_edges": daily_edges,
        "degree_distribution": degree_distribution,
        "density": density,
        "density_history": density_history,
        "orphan_counts": orphan_counts,
        "orphan_trend": orphan_trend,
        "total_orphans": total_orphans,
        "total_edges": totals.total_edges,
        "connected_nodes": totals.connected_nodes,
    }


# ── F021.1: Admission Control Dashboard ───────────────────────────────


async def get_admission_data(
    session: AsyncSession,
    agent_id: str,
    days: int = 30,
    threshold: float = 0.55,
    source: str | None = None,
    category: str | None = None,
) -> dict:
    """Return admission analytics: summary, histogram, dimensions, breakdowns, trends."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    # Base filter — all queries add active=true to exclude superseded facts
    base_where = "agent_id = :agent_id AND created_at >= :since AND active = true"
    params: dict = {"agent_id": agent_id, "since": since, "threshold": threshold}
    if source:
        base_where += " AND source = :source"
        params["source"] = source
    if category:
        base_where += " AND category = :category"
        params["category"] = category

    # ── Summary ──
    result = await session.execute(
        text(f"""
            SELECT
                COUNT(*) FILTER (WHERE admission_score IS NOT NULL AND admission_scores IS NOT NULL) AS total_scored,
                COUNT(*) FILTER (WHERE admission_score IS NOT NULL AND admission_scores IS NOT NULL AND admission_score >= :threshold) AS admitted,
                COUNT(*) FILTER (WHERE admission_score IS NOT NULL AND admission_scores IS NOT NULL AND admission_score < :threshold) AS would_reject,
                COUNT(*) FILTER (WHERE admission_score IS NOT NULL AND admission_scores IS NULL) AS bypassed,
                AVG(admission_score) FILTER (WHERE admission_scores IS NOT NULL) AS avg_score
            FROM heart.facts
            WHERE {base_where}
        """),
        params,
    )
    row = result.one()
    summary = {
        "total_scored": row.total_scored or 0,
        "admitted": row.admitted or 0,
        "would_reject": row.would_reject or 0,
        "bypassed": row.bypassed or 0,
        "avg_composite_score": round(float(row.avg_score), 3) if row.avg_score else 0.0,
        "rejection_rate": round(
            (row.would_reject or 0) / max(row.total_scored or 1, 1), 3
        ),
        "threshold_note": f"Counts based on current threshold ({threshold}). Actual scores were computed at admission time.",
        "_pre_migration_note": "Facts scored before migration 019 have admission_scores=NULL and are excluded from dimension/bypass stats.",
    }

    # ── Score distribution (0.05 buckets, cap at 0.95 for score=1.0) ──
    result = await session.execute(
        text(f"""
            SELECT
                LEAST(FLOOR(admission_score / 0.05) * 0.05, 0.95) AS bucket_start,
                COUNT(*) AS cnt
            FROM heart.facts
            WHERE {base_where}
              AND admission_score IS NOT NULL
              AND admission_scores IS NOT NULL
            GROUP BY bucket_start
            ORDER BY bucket_start
        """),
        params,
    )
    score_distribution = [
        {
            "bucket": f"{row.bucket_start:.2f}-{row.bucket_start + 0.05:.2f}",
            "count": row.cnt,
        }
        for row in result
    ]

    # ── Per-dimension stats (JSONB extraction) ──
    dimensions = ["utility", "confidence", "novelty", "recency", "type_prior"]
    dimension_stats: dict = {
        "_note": "Excludes bypassed facts (admission_scores IS NULL). Only available for facts scored after JSONB migration.",
    }
    for dim in dimensions:
        result = await session.execute(
            text(f"""
                WITH scored AS (
                    SELECT
                        (admission_scores->>'{dim}')::float AS val,
                        CASE WHEN admission_score >= :threshold THEN 'admitted' ELSE 'rejected' END AS status
                    FROM heart.facts
                    WHERE {base_where}
                      AND admission_scores IS NOT NULL
                      AND admission_scores->>'{dim}' IS NOT NULL
                )
                SELECT
                    status,
                    MIN(val) AS min_val,
                    percentile_cont(0.25) WITHIN GROUP (ORDER BY val) AS q1,
                    percentile_cont(0.50) WITHIN GROUP (ORDER BY val) AS median,
                    percentile_cont(0.75) WITHIN GROUP (ORDER BY val) AS q3,
                    MAX(val) AS max_val
                FROM scored
                GROUP BY status
            """),
            params,
        )
        dim_data: dict = {"admitted": {}, "rejected": {}}
        for row in result:
            dim_data[row.status] = {
                "min": round(float(row.min_val), 3),
                "q1": round(float(row.q1), 3),
                "median": round(float(row.median), 3),
                "q3": round(float(row.q3), 3),
                "max": round(float(row.max_val), 3),
            }
        dimension_stats[dim] = dim_data

    # ── By source ──
    result = await session.execute(
        text(f"""
            SELECT
                COALESCE(source, 'unknown') AS src,
                COUNT(*) FILTER (WHERE admission_scores IS NOT NULL AND admission_score >= :threshold) AS admitted,
                COUNT(*) FILTER (WHERE admission_scores IS NOT NULL AND admission_score < :threshold) AS rejected,
                COUNT(*) FILTER (WHERE admission_scores IS NULL AND admission_score IS NOT NULL) AS bypassed,
                AVG(admission_score) FILTER (WHERE admission_scores IS NOT NULL) AS avg_score
            FROM heart.facts
            WHERE {base_where} AND admission_score IS NOT NULL
            GROUP BY src ORDER BY src
        """),
        params,
    )
    by_source = {
        row.src: {
            "admitted": row.admitted or 0,
            "rejected": row.rejected or 0,
            "bypassed": row.bypassed or 0,
            "avg_score": round(float(row.avg_score), 3) if row.avg_score else None,
        }
        for row in result
    }

    # ── By category ──
    result = await session.execute(
        text(f"""
            SELECT
                COALESCE(category, 'uncategorized') AS cat,
                COUNT(*) FILTER (WHERE admission_scores IS NOT NULL AND admission_score >= :threshold) AS admitted,
                COUNT(*) FILTER (WHERE admission_scores IS NOT NULL AND admission_score < :threshold) AS rejected,
                AVG(admission_score) FILTER (WHERE admission_scores IS NOT NULL) AS avg_score
            FROM heart.facts
            WHERE {base_where} AND admission_score IS NOT NULL AND admission_scores IS NOT NULL
            GROUP BY cat ORDER BY cat
        """),
        params,
    )
    by_category = {
        row.cat: {
            "admitted": row.admitted or 0,
            "rejected": row.rejected or 0,
            "avg_score": round(float(row.avg_score), 3) if row.avg_score else None,
        }
        for row in result
    }

    # ── Daily trend (respects source/category filters) ──
    trend_join = "ON CAST(f.created_at AS date) = CAST(d AS date) AND f.agent_id = :agent_id AND f.admission_score IS NOT NULL AND f.active = true"
    trend_params: dict = {"agent_id": agent_id, "since": since, "now": now, "threshold": threshold}
    if source:
        trend_join += " AND f.source = :source"
        trend_params["source"] = source
    if category:
        trend_join += " AND f.category = :category"
        trend_params["category"] = category

    result = await session.execute(
        text(f"""
            SELECT
                CAST(d AS date) AS day,
                COUNT(f.id) FILTER (WHERE f.admission_scores IS NOT NULL) AS scored,
                COUNT(f.id) FILTER (WHERE f.admission_scores IS NOT NULL AND f.admission_score >= :threshold) AS admitted,
                COUNT(f.id) FILTER (WHERE f.admission_scores IS NOT NULL AND f.admission_score < :threshold) AS rejected,
                COUNT(f.id) FILTER (WHERE f.admission_scores IS NULL AND f.admission_score IS NOT NULL) AS bypassed,
                AVG(f.admission_score) FILTER (WHERE f.admission_scores IS NOT NULL) AS avg_score
            FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
            LEFT JOIN heart.facts f
                {trend_join}
            GROUP BY day ORDER BY day
        """),
        trend_params,
    )
    daily_trend = [
        {
            "date": row.day.isoformat(),
            "scored": row.scored or 0,
            "admitted": row.admitted or 0,
            "rejected": row.rejected or 0,
            "bypassed": row.bypassed or 0,
            "avg_score": round(float(row.avg_score), 3) if row.avg_score else None,
        }
        for row in result
    ]

    # ── Bypass breakdown ──
    result = await session.execute(
        text(f"""
            SELECT
                COALESCE(source, 'unknown') AS reason,
                COUNT(*) AS cnt
            FROM heart.facts
            WHERE {base_where}
              AND admission_score IS NOT NULL
              AND admission_scores IS NULL
            GROUP BY reason ORDER BY cnt DESC
        """),
        params,
    )
    bypass_breakdown = {row.reason: row.cnt for row in result}

    return {
        "summary": summary,
        "score_distribution": score_distribution,
        "dimension_stats": dimension_stats,
        "by_source": by_source,
        "by_category": by_category,
        "daily_trend": daily_trend,
        "bypass_breakdown": bypass_breakdown,
    }


# ── F021.1: Rejected facts list ───────────────────────────────────────


async def get_admission_rejected(
    session: AsyncSession,
    agent_id: str,
    threshold: float = 0.55,
    days: int = 30,
    limit: int = 50,
    offset: int = 0,
    sort: str = "admission_score",
    order: str = "asc",
) -> dict:
    """Return paginated list of facts below admission threshold."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    # Sort allowlist — includes spec alias "composite_score"
    ALLOWED_SORTS = {"admission_score", "created_at", "category", "source"}
    SORT_ALIASES = {"composite_score": "admission_score"}
    sort = SORT_ALIASES.get(sort, sort)
    if sort not in ALLOWED_SORTS:
        sort = "admission_score"
    if order not in ("asc", "desc"):
        order = "asc"

    base_where = """
        agent_id = :agent_id
        AND created_at >= :since
        AND admission_score IS NOT NULL
        AND admission_scores IS NOT NULL
        AND admission_score < :threshold
        AND active = true
    """

    # Total count
    result = await session.execute(
        text(f"SELECT COUNT(*) AS cnt FROM heart.facts WHERE {base_where}"),
        {"agent_id": agent_id, "since": since, "threshold": threshold},
    )
    total = result.scalar() or 0

    # Fetch page
    result = await session.execute(
        text(f"""
            SELECT
                id, content, category, source,
                admission_score, admission_scores, created_at
            FROM heart.facts
            WHERE {base_where}
            ORDER BY {sort} {order}
            LIMIT :limit OFFSET :offset
        """),
        {
            "agent_id": agent_id, "since": since, "threshold": threshold,
            "limit": limit, "offset": offset,
        },
    )
    facts = []
    for row in result:
        content = row.content or ""
        facts.append({
            "id": str(row.id),
            "content_preview": content[:200],
            "content_full": content,
            "category": row.category,
            "source": row.source,
            "composite_score": round(float(row.admission_score), 3),
            "scores": row.admission_scores or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })

    return {
        "facts": facts,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ── F024-3b: Rubric Dashboard ─────────────────────────────────────────


async def get_rubric_dashboard_data(
    session: AsyncSession, agent_id: str, settings: Any = None,
) -> dict:
    """Return rubric dashboard data: active rubric, signals, history, correlations, config."""
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    # Active rubric
    result = await session.execute(
        text("""
            SELECT id, version, status, change_reason, dimensions,
                   outcome_correlations, created_at
            FROM heart.rubric_versions
            WHERE agent_id = :agent_id AND status = 'active'
            LIMIT 1
        """),
        {"agent_id": agent_id},
    )
    active_row = result.one_or_none()
    active_rubric = None
    if active_row:
        dims = active_row.dimensions if isinstance(active_row.dimensions, list) else []
        active_rubric = {
            "version": active_row.version,
            "status": active_row.status,
            "dimension_count": len(dims),
            "created_at": active_row.created_at.isoformat() if active_row.created_at else None,
            "dimensions": dims,
        }

    # Version history
    result = await session.execute(
        text("""
            SELECT id, version, status, change_reason, dimensions, created_at
            FROM heart.rubric_versions
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT 20
        """),
        {"agent_id": agent_id},
    )
    version_history = []
    weight_history = []
    for row in result:
        dims = row.dimensions if isinstance(row.dimensions, list) else []
        version_history.append({
            "version": row.version,
            "status": row.status,
            "change_reason": row.change_reason,
            "dimension_count": len(dims),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
        weight_history.append({
            "version": row.version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "weights": {d["name"]: d["weight"] for d in dims if "name" in d and "weight" in d},
        })

    # Outcome signals — totals by type
    result = await session.execute(
        text("""
            SELECT signal_type, COUNT(*) AS cnt
            FROM heart.outcome_signals
            WHERE agent_id = :agent_id
            GROUP BY signal_type
        """),
        {"agent_id": agent_id},
    )
    by_type = {row.signal_type: row.cnt for row in result}
    total_signals = sum(by_type.values())

    # Outcome signals — recent 20
    result = await session.execute(
        text("""
            SELECT signal_type, confidence, evidence, created_at
            FROM heart.outcome_signals
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT 20
        """),
        {"agent_id": agent_id},
    )
    recent_signals = [
        {
            "signal_type": row.signal_type,
            "confidence": float(row.confidence) if row.confidence else 0.0,
            "evidence": row.evidence,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in result
    ]

    # Outcome signals — daily trend (last 30 days)
    result = await session.execute(
        text("""
            SELECT CAST(d AS date) AS date,
                   COUNT(*) FILTER (WHERE s.signal_type = 'completed') AS completed,
                   COUNT(*) FILTER (WHERE s.signal_type = 'corrected') AS corrected,
                   COUNT(*) FILTER (WHERE s.signal_type = 'praised') AS praised,
                   COUNT(*) FILTER (WHERE s.signal_type = 'reworked') AS reworked,
                   COUNT(*) FILTER (WHERE s.signal_type = 'self_corrected') AS self_corrected
            FROM generate_series(CAST(:since AS date), CAST(:now AS date), '1 day') AS d
            LEFT JOIN heart.outcome_signals s
                ON CAST(s.created_at AS date) = CAST(d AS date)
                AND s.agent_id = :agent_id
            GROUP BY CAST(d AS date)
            ORDER BY CAST(d AS date)
        """),
        {"agent_id": agent_id, "since": thirty_days_ago, "now": now},
    )
    daily_trend = [
        {
            "date": row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
            "completed": row.completed,
            "corrected": row.corrected,
            "praised": row.praised,
            "reworked": row.reworked,
            "self_corrected": row.self_corrected,
        }
        for row in result
    ]

    # Correlations from active rubric's stored data
    correlations_data = []
    correlation_sample = 0
    if active_row and active_row.outcome_correlations:
        oc = active_row.outcome_correlations
        for dim_name, signals in oc.items():
            if isinstance(signals, dict):
                for sig_type, stats in signals.items():
                    if isinstance(stats, dict):
                        correlations_data.append({
                            "dimension": dim_name,
                            "signal_type": sig_type,
                            "pearson_r": stats.get("pearson_r", 0),
                            "spearman_rho": stats.get("spearman_rho", 0),
                        })
                        correlation_sample = max(correlation_sample, stats.get("sample_size", 0))

    # Config
    config = {}
    if settings:
        config = {
            "rubric_enabled": getattr(settings, "rubric_enabled", False),
            "evolution_enabled": getattr(settings, "rubric_evolution_enabled", False),
            "outcome_detection_enabled": getattr(settings, "rubric_outcome_detection_enabled", False),
            "min_episodes_for_correlation": getattr(settings, "rubric_min_episodes_for_correlation", 50),
            "weight_change_cap": getattr(settings, "rubric_weight_change_cap", 0.05),
        }

    return {
        "active_rubric": active_rubric,
        "version_history": version_history,
        "outcome_signals": {
            "total": total_signals,
            "by_type": by_type,
            "recent": recent_signals,
            "daily_trend": daily_trend,
        },
        "correlations": {
            "data": correlations_data,
            "sample_size": correlation_sample,
        },
        "weight_history": weight_history,
        "config": config,
    }


# ── F034: Heartbeat dashboard data (GET /dashboard/heartbeat) ──────────


async def get_heartbeat_dashboard_data(
    session: AsyncSession, agent_id: str, hours: int = 24
) -> dict:
    """Return heartbeat tick history, cognitive sessions, and findings aggregates."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    seven_days_ago = now - timedelta(days=7)

    # Recent heartbeat_tick events (last N hours, limit 100)
    result = await session.execute(
        text("""
            SELECT event_type AS type, created_at, data
            FROM nous_system.events
            WHERE agent_id = :agent_id AND event_type = 'heartbeat_tick'
              AND created_at >= :since
            ORDER BY created_at DESC LIMIT 100
        """),
        {"agent_id": agent_id, "since": since},
    )
    tick_rows = result.fetchall()

    recent_ticks = []
    for row in tick_rows:
        recent_ticks.append({
            "created_at": row.created_at.isoformat(),
            "data": row.data,
        })

    # Recent heartbeat_triage events (cognitive sessions)
    result = await session.execute(
        text("""
            SELECT event_type AS type, created_at, data
            FROM nous_system.events
            WHERE agent_id = :agent_id AND event_type = 'heartbeat_triage'
              AND created_at >= :since
            ORDER BY created_at DESC LIMIT 20
        """),
        {"agent_id": agent_id, "since": since},
    )
    triage_rows = result.fetchall()

    cognitive_sessions = []
    for row in triage_rows:
        d = row.data or {}
        cognitive_sessions.append({
            "timestamp": row.created_at.isoformat(),
            "session_id": d.get("session_id"),
            "findings_count": d.get("findings_count", 0),
            "tokens_used": d.get("tokens_used", 0),
            "response_summary": d.get("response_summary", ""),
        })

    # Aggregate findings from tick events
    total_findings = 0
    merged_by_source: Counter[str] = Counter()
    merged_by_urgency: Counter[str] = Counter()
    all_findings_flat: list[dict] = []

    for row in tick_rows:
        d = row.data or {}
        total_findings += d.get("findings_count", 0)

        # Merge by_source / by_urgency dicts
        for src, cnt in (d.get("by_source") or {}).items():
            merged_by_source[src] += cnt
        for urg, cnt in (d.get("by_urgency") or {}).items():
            merged_by_urgency[urg] += cnt

        # Flatten individual findings for timeline
        for f in d.get("findings") or []:
            all_findings_flat.append({
                "source": f.get("source"),
                "summary": f.get("summary"),
                "urgency": f.get("urgency"),
                "check_name": f.get("check_name"),
                "timestamp": row.created_at.isoformat(),
            })

    # Cap timeline to most recent 50
    findings_timeline = all_findings_flat[:50]

    findings_summary = {
        "total": total_findings,
        "by_source": dict(merged_by_source),
        "by_urgency": dict(merged_by_urgency),
    }

    # Findings by day (last 7 days) — aggregate by_urgency from tick events
    daily_urgency: dict[str, Counter[str]] = defaultdict(Counter)
    daily_counts: dict[str, int] = defaultdict(int)
    for row in tick_rows:
        d = row.data or {}
        day_key = row.created_at.date().isoformat()
        daily_counts[day_key] += d.get("findings_count", 0)
        for urg, cnt in (d.get("by_urgency") or {}).items():
            daily_urgency[day_key][urg] += cnt

    # Build 7-day array with zero-fills
    findings_by_day = []
    for i in range(7):
        day = (seven_days_ago.date() + timedelta(days=i)).isoformat()
        urg = daily_urgency.get(day, Counter())
        findings_by_day.append({
            "date": day,
            "findings_count": daily_counts.get(day, 0),
            "by_urgency": {"high": urg.get("high", 0), "normal": urg.get("normal", 0), "low": urg.get("low", 0)},
        })

    return {
        "recent_ticks": recent_ticks,
        "cognitive_sessions": cognitive_sessions,
        "totals": findings_summary,
        "findings_by_day": findings_by_day,
        "findings_timeline": findings_timeline,
    }


# ── F038: DAG Orchestration Dashboard ──────────────────────────────────────


def _dag_iso(val: Any) -> str | None:
    """Convert a datetime or string to ISO string, or None."""
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


async def get_dag_dashboard_data(session: AsyncSession, agent_id: str) -> dict[str, Any]:
    """Return DAG orchestration dashboard data: active DAGs, recent DAGs, stats."""
    now = datetime.now(timezone.utc)
    twenty_four_hours_ago = now - timedelta(hours=24)

    # ── Active DAGs (pending / running) ──
    result = await session.execute(
        text("""
            SELECT id, name, description, status, source, created_at,
                   started_at, token_budget, tokens_consumed
            FROM nous_system.execution_dags
            WHERE agent_id = :agent_id AND status IN ('pending', 'running')
            ORDER BY created_at DESC
        """),
        {"agent_id": agent_id},
    )
    active_dag_rows = result.all()

    active_dags: list[dict] = []
    for dag_row in active_dag_rows:
        dag_id_str = str(dag_row.id)
        dag_id_uuid = dag_row.id  # Keep UUID for SQL binds

        # Nodes for this DAG
        node_result = await session.execute(
            text("""
                SELECT id, name, description, node_type, wave, status,
                       result, error, tokens_used, started_at, completed_at
                FROM nous_system.dag_nodes
                WHERE dag_id = :dag_id
                ORDER BY wave, name
            """),
            {"dag_id": dag_id_uuid},
        )
        nodes = []
        for n in node_result:
            nodes.append({
                "id": str(n.id),
                "name": n.name,
                "description": n.description or "",
                "node_type": n.node_type,
                "wave": n.wave,
                "status": n.status,
                "result": (n.result or "")[:200],
                "error": (n.error or "")[:200],
                "tokens_used": n.tokens_used or 0,
                "started_at": _dag_iso(n.started_at),
                "completed_at": _dag_iso(n.completed_at),
            })

        # Edges for this DAG
        edge_result = await session.execute(
            text("""
                SELECT id, from_node_id, to_node_id, edge_type
                FROM nous_system.dag_edges
                WHERE dag_id = :dag_id
            """),
            {"dag_id": dag_id_uuid},
        )
        edges = [
            {
                "id": str(e.id),
                "from_node_id": str(e.from_node_id),
                "to_node_id": str(e.to_node_id),
                "edge_type": e.edge_type,
            }
            for e in edge_result
        ]

        active_dags.append({
            "id": dag_id_str,
            "name": dag_row.name,
            "description": dag_row.description or "",
            "status": dag_row.status,
            "source": dag_row.source,
            "created_at": _dag_iso(dag_row.created_at),
            "started_at": _dag_iso(dag_row.started_at),
            "token_budget": dag_row.token_budget,
            "tokens_consumed": dag_row.tokens_consumed or 0,
            "nodes": nodes,
            "edges": edges,
        })

    # ── Recent completed/failed/cancelled DAGs (last 20) ──
    result = await session.execute(
        text("""
            SELECT d.id, d.name, d.status, d.source, d.created_at, d.completed_at,
                   d.token_budget, d.tokens_consumed, d.result_summary, d.postmortem,
                   (SELECT COUNT(*) FROM nous_system.dag_nodes n WHERE n.dag_id = d.id) AS node_count,
                   (SELECT COUNT(*) FROM nous_system.dag_nodes n WHERE n.dag_id = d.id AND n.status = 'completed') AS completed_count
            FROM nous_system.execution_dags d
            WHERE d.agent_id = :agent_id AND d.status IN ('completed', 'failed', 'cancelled', 'partial')
            ORDER BY d.completed_at DESC NULLS LAST
            LIMIT 20
        """),
        {"agent_id": agent_id},
    )
    recent_dags = [
        {
            "id": str(row.id),
            "name": row.name,
            "status": row.status,
            "source": row.source,
            "created_at": _dag_iso(row.created_at),
            "completed_at": _dag_iso(row.completed_at),
            "token_budget": row.token_budget,
            "tokens_consumed": row.tokens_consumed or 0,
            "result_summary": row.result_summary,
            "postmortem": row.postmortem,
            "node_count": row.node_count,
            "completed_count": row.completed_count,
        }
        for row in result
    ]

    # ── Stats ──
    # Active count
    result = await session.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM nous_system.execution_dags
            WHERE agent_id = :agent_id AND status IN ('pending', 'running')
        """),
        {"agent_id": agent_id},
    )
    active_count = result.scalar_one()

    # Nodes completed in 24h
    result = await session.execute(
        text("""
            SELECT COUNT(*) AS cnt
            FROM nous_system.dag_nodes n
            JOIN nous_system.execution_dags d ON d.id = n.dag_id
            WHERE d.agent_id = :agent_id
              AND n.status = 'completed'
              AND n.completed_at >= :since
        """),
        {"agent_id": agent_id, "since": twenty_four_hours_ago},
    )
    nodes_completed_24h = result.scalar_one()

    # Success rate: count completed vs total finished
    result = await session.execute(
        text("""
            SELECT
                COUNT(*) AS total_finished,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
            FROM nous_system.execution_dags
            WHERE agent_id = :agent_id AND status IN ('completed', 'failed', 'cancelled', 'partial')
        """),
        {"agent_id": agent_id},
    )
    stats_row = result.one()
    total_finished = stats_row.total_finished or 0
    completed_count_stat = stats_row.completed or 0
    success_rate = (completed_count_stat / total_finished) if total_finished > 0 else 0.0

    # Average completion time — compute in Python for cross-DB compat
    result = await session.execute(
        text("""
            SELECT created_at, completed_at
            FROM nous_system.execution_dags
            WHERE agent_id = :agent_id AND completed_at IS NOT NULL
              AND status IN ('completed', 'failed', 'cancelled', 'partial')
        """),
        {"agent_id": agent_id},
    )
    durations: list[float] = []
    for row in result:
        try:
            ca = row.created_at if hasattr(row.created_at, 'timestamp') else datetime.fromisoformat(str(row.created_at))
            co = row.completed_at if hasattr(row.completed_at, 'timestamp') else datetime.fromisoformat(str(row.completed_at))
            durations.append((co - ca).total_seconds())
        except Exception:
            pass
    avg_seconds = sum(durations) / len(durations) if durations else 0.0

    return {
        "active_dags": active_dags,
        "recent_dags": recent_dags,
        "stats": {
            "active_count": active_count,
            "nodes_completed_24h": nodes_completed_24h,
            "success_rate": round(success_rate, 3),
            "avg_completion_seconds": round(avg_seconds, 1),
        },
    }


# ── F040: Graph density dashboard ─────────────────────────────────────────


async def get_density_data(session: AsyncSession, agent_id: str) -> dict:
    """F040: Return graph density metrics for the density dashboard tab."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # Per-type orphan and total counts. The active-filter is derived from the
    # shared _ACTIVE_FILTER_TABLES set (not hardcoded per row) so total and
    # orphan denominators stay consistent and never drift from the graph/health
    # orphan sites. Excludes soft-deleted nodes — notably deactivated raw
    # episode transcripts, the dominant orphan class.
    type_configs = [
        ("fact", "heart.facts", "fact"),
        ("decision", "brain.decisions", "decision"),
        ("episode", "heart.episodes", "episode"),
        ("procedure", "heart.procedures", "procedure"),
        # F067: chunks are matched via source/target_type = 'chunk' on
        # F070's part_of / summarized_by / related_to edges.
        ("chunk", "heart.episode_chunks", "chunk"),
    ]

    total_nodes = 0
    total_orphans = 0
    density_by_type: dict[str, dict[str, Any]] = {}

    for type_name, table, edge_type in type_configs:
        active = _active_clause(table)
        # Total count for this type
        result = await session.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM {table} t
                WHERE t.agent_id = :agent_id{active}
            """),
            {"agent_id": agent_id},
        )
        type_total = result.scalar() or 0

        # Orphan count: nodes with no edges referencing them
        result = await session.execute(
            text(f"""
                SELECT COUNT(*) AS cnt
                FROM {table} t
                WHERE t.agent_id = :agent_id{active}
                  AND NOT EXISTS (
                      SELECT 1 FROM brain.graph_edges e
                      WHERE e.agent_id = :agent_id
                        AND (
                            (e.source_id = t.id AND e.source_type = :edge_type)
                            OR (e.target_id = t.id AND e.target_type = :edge_type)
                        )
                  )
            """),
            {"agent_id": agent_id, "edge_type": edge_type},
        )
        type_orphans = result.scalar() or 0

        total_nodes += type_total
        total_orphans += type_orphans
        density_by_type[type_name] = {
            "total": type_total,
            "orphan": type_orphans,
            "orphan_rate": round(type_orphans / type_total, 4) if type_total > 0 else 0.0,
        }

    # Total edges
    result = await session.execute(
        text("SELECT COUNT(*) FROM brain.graph_edges WHERE agent_id = :agent_id"),
        {"agent_id": agent_id},
    )
    total_edges = result.scalar() or 0

    # Edge distribution by relation type
    result = await session.execute(
        text("""
            SELECT relation, COUNT(*) AS cnt
            FROM brain.graph_edges
            WHERE agent_id = :agent_id
            GROUP BY relation
            ORDER BY cnt DESC
        """),
        {"agent_id": agent_id},
    )
    edge_distribution = {row.relation: row.cnt for row in result}

    # Connected nodes (unique nodes that appear in at least one edge)
    result = await session.execute(
        text("""
            SELECT COUNT(DISTINCT node_id) AS cnt FROM (
                SELECT source_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
                UNION
                SELECT target_id AS node_id FROM brain.graph_edges WHERE agent_id = :agent_id
            ) sub
        """),
        {"agent_id": agent_id},
    )
    connected_nodes = result.scalar() or 0

    # Average degree
    avg_degree = round(total_edges / connected_nodes, 2) if connected_nodes > 0 else 0.0

    # Backfill progress: auto-linked edges per day over last 7 days
    result = await session.execute(
        text("""
            SELECT DATE(created_at) AS day, COUNT(*) AS cnt
            FROM brain.graph_edges
            WHERE agent_id = :agent_id
              AND auto_linked = true
              AND created_at >= :since
            GROUP BY day
            ORDER BY day
        """),
        {"agent_id": agent_id, "since": seven_days_ago},
    )
    backfill_progress = [
        {"date": str(row.day), "edges": row.cnt}
        for row in result
    ]

    orphan_rate = round(total_orphans / total_nodes, 4) if total_nodes > 0 else 0.0

    return {
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "total_orphans": total_orphans,
        "orphan_rate": orphan_rate,
        "avg_degree": avg_degree,
        "connected_nodes": connected_nodes,
        "density_by_type": density_by_type,
        "edge_distribution": edge_distribution,
        "backfill_progress": backfill_progress,
    }


# ── F061: Subtask Hardening Dashboard ──────────────────────────────────────


async def get_subtask_dashboard_data(
    session: AsyncSession, agent_id: str, hours: int = 24
) -> dict[str, Any]:
    """F061: subtask outcome metrics for the /dashboard/subtasks tab.

    All cards are produced via SQL aggregations (no Python row loops). One
    extra query against ``nous_system.events`` for the daily-trend card so
    operators can see empty-rate evolving over time.

    Cards:
      - ``totals``         — counts grouped by ``final_outcome``
      - ``empty_rate``     — (incomplete_no_terminal + validation_failed) / total
      - ``retry_rate``     — fraction of subtasks where ``attempts > 1``
      - ``tokens_by_outcome`` — mean ``tokens_in + tokens_out`` per outcome
      - ``top_failing_tasks`` — group by ``task[:80]``, highest failure rate
      - ``dag_correlation`` — counts of subtasks tied to DAG nodes
      - ``recent_outcomes`` — last 50 terminal subtasks with their outcome
      - ``daily_trend``    — daily counts grouped by outcome (window length)
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    # Card 1: outcome counts
    result = await session.execute(
        text("""
            SELECT
                COALESCE(final_outcome, 'unknown') AS outcome,
                COUNT(*) AS cnt
            FROM heart.subtasks
            WHERE agent_id = :agent_id
              AND completed_at IS NOT NULL
              AND completed_at >= :since
            GROUP BY COALESCE(final_outcome, 'unknown')
            ORDER BY cnt DESC
        """),
        {"agent_id": agent_id, "since": since},
    )
    by_outcome: dict[str, int] = {row.outcome: row.cnt for row in result}
    total_terminal = sum(by_outcome.values())

    # Card 2/3: derived rates
    failed_outcomes = {"incomplete_no_terminal", "validation_failed"}
    failure_count = sum(
        cnt for outcome, cnt in by_outcome.items() if outcome in failed_outcomes
    )
    empty_rate = (failure_count / total_terminal) if total_terminal > 0 else 0.0

    # Retry rate: completed_at-bounded rows where attempts > 1
    result = await session.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE attempts > 1) AS retried,
                COUNT(*) AS total
            FROM heart.subtasks
            WHERE agent_id = :agent_id
              AND completed_at IS NOT NULL
              AND completed_at >= :since
        """),
        {"agent_id": agent_id, "since": since},
    )
    row = result.first()
    retried = row.retried or 0
    retry_rate = (retried / row.total) if row.total else 0.0

    # Card 4: tokens by outcome
    result = await session.execute(
        text("""
            SELECT
                COALESCE(final_outcome, 'unknown') AS outcome,
                AVG(tokens_in + tokens_out)::INTEGER AS mean_total_tokens,
                AVG(tool_calls_made)::FLOAT AS mean_tool_calls,
                COUNT(*) AS n
            FROM heart.subtasks
            WHERE agent_id = :agent_id
              AND completed_at IS NOT NULL
              AND completed_at >= :since
            GROUP BY COALESCE(final_outcome, 'unknown')
        """),
        {"agent_id": agent_id, "since": since},
    )
    tokens_by_outcome = {
        r.outcome: {
            "mean_total_tokens": r.mean_total_tokens or 0,
            "mean_tool_calls": round(r.mean_tool_calls or 0, 2),
            "n": r.n,
        }
        for r in result
    }

    # Card 5: top failing tasks (group by task[:80]).
    # Sort by failure RATE first, then absolute failures — surfaces tasks
    # that fail consistently even at low volume (e.g., a 100% failure rate
    # template) rather than burying them under high-volume tasks with
    # incidental failures (review P2-G).
    result = await session.execute(
        text("""
            SELECT
                LEFT(task, 80) AS task_prefix,
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE final_outcome IN ('incomplete_no_terminal', 'validation_failed', 'errored', 'timed_out')
                ) AS failures
            FROM heart.subtasks
            WHERE agent_id = :agent_id
              AND completed_at IS NOT NULL
              AND completed_at >= :since
            GROUP BY LEFT(task, 80)
            HAVING COUNT(*) FILTER (
                WHERE final_outcome IN ('incomplete_no_terminal', 'validation_failed', 'errored', 'timed_out')
            ) > 0
            ORDER BY (
                COUNT(*) FILTER (
                    WHERE final_outcome IN ('incomplete_no_terminal', 'validation_failed', 'errored', 'timed_out')
                )::float / NULLIF(COUNT(*), 0)
            ) DESC, failures DESC, total DESC
            LIMIT 10
        """),
        {"agent_id": agent_id, "since": since},
    )
    top_failing = [
        {
            "task_prefix": r.task_prefix,
            "total": r.total,
            "failures": r.failures,
            "failure_rate": round(r.failures / r.total, 3) if r.total else 0.0,
        }
        for r in result
    ]

    # Card 6: DAG correlation
    result = await session.execute(
        text("""
            SELECT
                COALESCE(final_outcome, 'unknown') AS outcome,
                COUNT(*) AS cnt
            FROM heart.subtasks
            WHERE agent_id = :agent_id
              AND dag_node_id IS NOT NULL
              AND completed_at IS NOT NULL
              AND completed_at >= :since
            GROUP BY COALESCE(final_outcome, 'unknown')
        """),
        {"agent_id": agent_id, "since": since},
    )
    dag_correlation = {row.outcome: row.cnt for row in result}

    # Card 7: recent terminal subtasks (most recent 50)
    result = await session.execute(
        text("""
            SELECT
                id,
                LEFT(task, 80) AS task,
                final_outcome,
                attempts,
                tokens_in,
                tokens_out,
                tool_calls_made,
                completed_at,
                dag_node_id
            FROM heart.subtasks
            WHERE agent_id = :agent_id
              AND completed_at IS NOT NULL
              AND completed_at >= :since
            ORDER BY completed_at DESC
            LIMIT 50
        """),
        {"agent_id": agent_id, "since": since},
    )
    recent_outcomes = [
        {
            "id": str(r.id),
            "task": r.task,
            "final_outcome": r.final_outcome,
            "attempts": r.attempts,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "tool_calls_made": r.tool_calls_made,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "dag_node_id": str(r.dag_node_id) if r.dag_node_id else None,
        }
        for r in result
    ]

    # Card 8: daily trend — uses the SAME `since` as the other cards so the
    # window stays consistent (e.g., 30h window shows a 30h trend, not 48h).
    result = await session.execute(
        text("""
            SELECT
                DATE(completed_at) AS day,
                COALESCE(final_outcome, 'unknown') AS outcome,
                COUNT(*) AS cnt
            FROM heart.subtasks
            WHERE agent_id = :agent_id
              AND completed_at IS NOT NULL
              AND completed_at >= :since
            GROUP BY DATE(completed_at), COALESCE(final_outcome, 'unknown')
            ORDER BY day
        """),
        {"agent_id": agent_id, "since": since},
    )
    daily_buckets: dict[str, dict[str, int]] = {}
    for r in result:
        day_key = r.day.isoformat()
        daily_buckets.setdefault(day_key, {})[r.outcome] = r.cnt
    daily_trend = [
        {"date": day, "by_outcome": buckets}
        for day, buckets in sorted(daily_buckets.items())
    ]

    return {
        "window_hours": hours,
        "totals": {
            "total_terminal": total_terminal,
            "by_outcome": by_outcome,
            "empty_rate": round(empty_rate, 4),
            "retry_rate": round(retry_rate, 4),
        },
        "tokens_by_outcome": tokens_by_outcome,
        "top_failing_tasks": top_failing,
        "dag_correlation": dag_correlation,
        "recent_outcomes": recent_outcomes,
        "daily_trend": daily_trend,
    }
