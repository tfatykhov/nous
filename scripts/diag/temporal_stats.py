"""Quick event_date / happened_before health stats for a DB (env DB_* selects it).

Used before/after a --reclassify run to quantify the F075 extraction fix:
  - bibliographic suspects = first-of-month event_dates (month-only pub dates)
  - wrong-year suspects     = event_date year < learned_at year
  - happened_before edge count + first-of-month chain count

Run:
    DB_HOST=localhost DB_PORT=5433 DB_USER=nous DB_PASSWORD=nous_eval \\
    DB_NAME=nous_eval_prod PYTHONPATH=. uv run python scripts/diag/temporal_stats.py
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

DB = dict(host=os.environ.get("DB_HOST", "localhost"),
          port=int(os.environ.get("DB_PORT", "5433")),
          user=os.environ.get("DB_USER", "nous"),
          password=os.environ.get("DB_PASSWORD", "nous_eval"),
          database=os.environ.get("DB_NAME", "nous_eval_prod"))
AGENT = os.environ.get("AGENT", "nous-default")


async def main() -> None:
    conn = await asyncpg.connect(**DB)
    try:
        dated = await conn.fetchval(
            "SELECT count(*) FROM heart.facts WHERE agent_id=$1 AND active "
            "AND event_date IS NOT NULL", AGENT)
        first_of_month = await conn.fetchval(
            "SELECT count(*) FROM heart.facts WHERE agent_id=$1 AND active "
            "AND event_date IS NOT NULL AND extract(day FROM event_date)=1", AGENT)
        wrong_year = await conn.fetchval(
            "SELECT count(*) FROM heart.facts WHERE agent_id=$1 AND active "
            "AND event_date IS NOT NULL AND learned_at IS NOT NULL "
            "AND extract(year FROM event_date) < extract(year FROM learned_at)", AGENT)
        edges = await conn.fetchval(
            "SELECT count(*) FROM brain.graph_edges WHERE agent_id=$1 "
            "AND relation='happened_before'", AGENT)
        edge_fom = await conn.fetchval(
            """SELECT count(*) FROM brain.graph_edges e
               JOIN heart.facts a ON a.id=e.source_id
               WHERE e.agent_id=$1 AND e.relation='happened_before'
                 AND extract(day FROM a.event_date)=1""", AGENT)
        print(f"DB={DB['database']}:{DB['port']}  agent={AGENT}")
        print(f"  dated facts:            {dated}")
        print(f"  first-of-month (suspect): {first_of_month}  ({pct(first_of_month, dated)})")
        print(f"  wrong-year (suspect):     {wrong_year}  ({pct(wrong_year, dated)})")
        print(f"  happened_before edges:  {edges}")
        print(f"    from first-of-month src: {edge_fom}  ({pct(edge_fom, edges)})")
    finally:
        await conn.close()


def pct(n: int, d: int) -> str:
    return f"{100*n/d:.0f}%" if d else "n/a"


if __name__ == "__main__":
    asyncio.run(main())
