"""Read-only report: graph edges a deliberated decision got while it still held its placeholder text.

A deliberated decision is recorded as "Plan: <user message>" and linked on that text: Brain._auto_link
writes related_to edges to other decisions in the same transaction, and DecisionGraphLinker writes
evidence_for (fact -> decision) and discussed_in (episode -> decision) edges right after the commit.
The real description arrives later, at finalize. With decision_relink_on_update_enabled the decision
is linked again on the real text, but the placeholder's edges stay. This report sizes them; it writes
nothing (the transaction is READ ONLY and rolled back) and has no option that deletes.

For each relation it counts the auto-linked edges of deliberated decisions created within
--window-minutes of the decision, and how many of them now fall below the threshold that admitted
them, measured on the stored embeddings (the final text's vector for the decision):

- related_to: exact. _auto_link compares the stored decision embeddings against auto_link_threshold.
- evidence_for / discussed_in: approximate. The linker compares embeddings of "fact: ..." /
  "episode: ..." / "decision: ..." template text, not the stored vectors, against
  graph_threshold_fact_decision / graph_threshold_fact_episode.

"below, reinforced" counts the ones with ltp_count > 0. A deliberated decision is one that carries
DeliberationEngine.start's capture reason.

The counts are a ceiling for turns that end within the window, not an exact split: every edge
created in the window counts, whichever text produced it (a backed-up bus can run the linker after
finalize), a turn longer than the window is undercounted, and a related_to edge between two
deliberated decisions is admitted by either endpoint's window. Run it before
decision_relink_on_update_enabled is on: after, the re-link's own edges on the final text land in the
same window and are counted too.

Usage::

    uv run python scripts/ops/report_placeholder_link_edges.py --agent-id nous-default
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from nous.cognitive.deliberation import _CAPTURE_REASON_MARK, _CAPTURE_REASON_PREFIX
from nous.config import Settings
from nous.storage.database import Database

_SQL = text("""
    WITH deliberated AS (
        SELECT d.id, d.created_at, d.embedding
        FROM brain.decisions d
        WHERE d.agent_id = :agent_id
          AND d.embedding IS NOT NULL
          AND EXISTS (SELECT 1 FROM brain.decision_reasons r
                      WHERE r.decision_id = d.id AND r.text LIKE :marker)
    ),
    placeholder_edges AS (
        SELECT DISTINCT ON (g.id)
               g.id, g.relation, g.ltp_count,
               1 - (dl.embedding <=> COALESCE(od.embedding, f.embedding, ep.embedding)) AS cosine,
               CASE g.relation WHEN 'related_to' THEN CAST(:t_related AS float8)
                               WHEN 'evidence_for' THEN CAST(:t_evidence AS float8)
                               ELSE CAST(:t_discussed AS float8) END AS threshold
        FROM brain.graph_edges g
        JOIN deliberated dl ON dl.id IN (g.source_id, g.target_id)
        LEFT JOIN brain.decisions od
               ON g.relation = 'related_to'
              AND od.id = CASE WHEN g.source_id = dl.id THEN g.target_id ELSE g.source_id END
        LEFT JOIN heart.facts f ON g.relation = 'evidence_for' AND f.id = g.source_id
        LEFT JOIN heart.episodes ep ON g.relation = 'discussed_in' AND ep.id = g.source_id
        WHERE g.agent_id = :agent_id
          AND g.auto_linked
          AND (
                (g.relation = 'related_to' AND g.source_type = 'decision' AND g.target_type = 'decision')
             OR (g.relation = 'evidence_for' AND g.source_type = 'fact' AND g.target_id = dl.id)
             OR (g.relation = 'discussed_in' AND g.source_type = 'episode' AND g.target_id = dl.id)
          )
          AND g.created_at >= dl.created_at
          AND g.created_at < dl.created_at + make_interval(mins => CAST(:window AS integer))
        ORDER BY g.id
    )
    SELECT relation,
           count(*) AS edges,
           count(cosine) AS measured,
           count(*) FILTER (WHERE cosine < threshold) AS below,
           count(*) FILTER (WHERE cosine < threshold AND ltp_count > 0) AS below_reinforced
    FROM placeholder_edges
    GROUP BY relation
    ORDER BY relation
""")


async def report(agent_id: str, window_minutes: int) -> None:
    """Print the counts; reads only."""
    settings = Settings()
    thresholds = {
        "related_to": (settings.auto_link_threshold, "exact"),
        "evidence_for": (settings.graph_threshold_fact_decision, "approximate"),
        "discussed_in": (settings.graph_threshold_fact_episode, "approximate"),
    }
    db = Database(settings)
    await db.connect()
    try:
        async with db.engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = (await conn.execute(_SQL, {
                "agent_id": agent_id,
                "marker": f"{_CAPTURE_REASON_PREFIX}%{_CAPTURE_REASON_MARK}%",
                "window": window_minutes,
                "t_related": thresholds["related_to"][0],
                "t_evidence": thresholds["evidence_for"][0],
                "t_discussed": thresholds["discussed_in"][0],
            })).all()
            await conn.rollback()
    finally:
        await db.disconnect()

    print(f"agent {agent_id}: auto-linked edges of deliberated decisions created within {window_minutes} min")
    header = f"{'relation':<14}{'threshold':>10}{'edges':>8}{'measured':>10}{'below':>8}{'below, reinforced':>19}"
    print(f"{header}  measure")
    by_relation = {row.relation: row for row in rows}
    for relation, (threshold, measure) in thresholds.items():
        row = by_relation.get(relation)
        edges, measured, below, reinforced = (
            (row.edges, row.measured, row.below, row.below_reinforced) if row else (0, 0, 0, 0)
        )
        print(f"{relation:<14}{threshold:>10.2f}{edges:>8}{measured:>10}{below:>8}{reinforced:>19}  {measure}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", default=None, help="Agent to report on (default: NOUS_AGENT_ID).")
    parser.add_argument(
        "--window-minutes", type=int, default=10,
        help="Edges created this long after the decision count as placeholder-era (default 10).",
    )
    args = parser.parse_args()
    asyncio.run(report(args.agent_id or Settings().agent_id, args.window_minutes))


if __name__ == "__main__":
    main()
