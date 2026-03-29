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
                SELECT id, created_at FROM heart.episodes WHERE agent_id = :agent_id
                UNION ALL
                SELECT id, created_at FROM heart.procedures WHERE agent_id = :agent_id AND active = true
            ),
            daily_new_total AS (
                SELECT CAST(created_at AS date) AS day, COUNT(*) AS cnt
                FROM all_nodes
                GROUP BY CAST(created_at AS date)
            ),
            daily_new_connected AS (
                SELECT day, COUNT(DISTINCT node_id) AS cnt
                FROM (
                    SELECT CAST(created_at AS date) AS day, source_id AS node_id
                    FROM brain.graph_edges WHERE agent_id = :agent_id
                    UNION
                    SELECT CAST(created_at AS date) AS day, target_id AS node_id
                    FROM brain.graph_edges WHERE agent_id = :agent_id
                ) first_seen
                GROUP BY day
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
