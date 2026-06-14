"""Diagnose happened_before edge precision (audit 2026-06-13 = 0.27).

Pulls a sample of prod happened_before edges with BOTH facts' content +
event_dates + source_episode, so we can eyeball whether:
  (a) the event_dates are sane (real event dates, not mention/conversation dates),
  (b) the two chained facts are narratively related (vs arbitrary co-episode dated facts).

Run:
    PROD_PW=... PYTHONPATH=. uv run python scripts/diag/probe_happened_before.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROD = dict(host=os.environ.get("PROD_HOST", "192.168.1.141"), port=5432,
            user="nous", password=os.environ["PROD_PW"], database="nous")
AGENT = "nous-default"


async def main() -> None:
    conn = await asyncpg.connect(**PROD)
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM brain.graph_edges "
            "WHERE agent_id=$1 AND relation='happened_before'", AGENT)
        print(f"total happened_before edges: {n}\n")

        # event_date population on facts (the substrate)
        tot = await conn.fetchval(
            "SELECT count(*) FROM heart.facts WHERE agent_id=$1 AND active=true", AGENT)
        dated = await conn.fetchval(
            "SELECT count(*) FROM heart.facts WHERE agent_id=$1 AND active=true "
            "AND event_date IS NOT NULL", AGENT)
        print(f"active facts: {tot}  with event_date: {dated} "
              f"({100*dated/tot:.1f}%)\n")

        # How many distinct event_dates per linked episode? (concurrency = noise risk)
        print("=== sample of 25 happened_before edges (A happened_before B) ===\n")
        rows = await conn.fetch(
            """
            SELECT a.content AS a_content, a.event_date AS a_date,
                   b.content AS b_content, b.event_date AS b_date,
                   a.source_episode_id AS ep,
                   a.created_at::date AS a_learned, b.created_at::date AS b_learned
            FROM brain.graph_edges e
            JOIN heart.facts a ON a.id = e.source_id
            JOIN heart.facts b ON b.id = e.target_id
            WHERE e.agent_id = $1 AND e.relation = 'happened_before'
            ORDER BY random()
            LIMIT 25
            """, AGENT)
        for i, r in enumerate(rows, 1):
            print(f"--- {i}. episode {str(r['ep'])[:8]} ---")
            print(f"  A [{r['a_date']}] (learned {r['a_learned']}): {r['a_content'][:160]}")
            print(f"  B [{r['b_date']}] (learned {r['b_learned']}): {r['b_content'][:160]}")
            gap = (r['b_date'] - r['a_date']).days if r['a_date'] and r['b_date'] else None
            print(f"  date gap: {gap} days\n")

        # Distribution: how big is the within-episode date gap? Tiny gaps + same
        # learned-date hint event_date == mention date (extraction failure mode).
        print("=== within-edge event_date gap distribution (days) ===")
        gaps = await conn.fetch(
            """
            SELECT (b.event_date - a.event_date) AS gap_days, count(*) n
            FROM brain.graph_edges e
            JOIN heart.facts a ON a.id = e.source_id
            JOIN heart.facts b ON b.id = e.target_id
            WHERE e.agent_id = $1 AND e.relation = 'happened_before'
            GROUP BY 1 ORDER BY n DESC LIMIT 15
            """, AGENT)
        for r in gaps:
            print(f"  gap={r['gap_days']:>5} days   n={r['n']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
