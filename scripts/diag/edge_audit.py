"""Prod edge-distribution audit: counts by relation_type x extraction_method,
plus node-type totals, to investigate low/missing edge classes (esp. contradicts).

Run:
    PROD_PW=... PYTHONPATH=. uv run python scripts/diag/edge_audit.py
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

PROD = dict(host=os.environ.get("PROD_HOST", "192.168.1.141"), port=5432,
            user="nous", password=os.environ["PROD_PW"], database="nous")
AGENT = "nous-default"


async def main() -> None:
    conn = await asyncpg.connect(**PROD)
    try:
        print("=== graph_edges by relation ===")
        rows = await conn.fetch(
            "SELECT relation, count(*) n FROM brain.graph_edges "
            "WHERE agent_id=$1 GROUP BY relation ORDER BY n DESC", AGENT)
        for r in rows:
            print(f"  {r['relation']:<24} {r['n']:>7}")

        print("\n=== graph_edges by extraction_method ===")
        rows = await conn.fetch(
            "SELECT extraction_method, count(*) n FROM brain.graph_edges "
            "WHERE agent_id=$1 GROUP BY extraction_method ORDER BY n DESC", AGENT)
        for r in rows:
            print(f"  {str(r['extraction_method']):<24} {r['n']:>7}")

        print("\n=== relation x source/target node type ===")
        rows = await conn.fetch(
            "SELECT relation, source_type, target_type, count(*) n "
            "FROM brain.graph_edges WHERE agent_id=$1 "
            "GROUP BY relation, source_type, target_type ORDER BY n DESC LIMIT 40", AGENT)
        for r in rows:
            print(f"  {r['relation']:<20} {str(r['source_type']):<10}->{str(r['target_type']):<10} {r['n']:>7}")

        print("\n=== node totals ===")
        for tbl in ("heart.facts", "heart.episodes", "brain.decisions",
                    "heart.procedures", "heart.episode_chunks"):
            try:
                n = await conn.fetchval(f"SELECT count(*) FROM {tbl} WHERE agent_id=$1", AGENT)
                act = await conn.fetchval(
                    f"SELECT count(*) FROM {tbl} WHERE agent_id=$1 AND active=true", AGENT)
                print(f"  {tbl:<24} total={n:>7}  active={act:>7}")
            except Exception as e:  # noqa: BLE001
                print(f"  {tbl:<24} ERR {type(e).__name__}: {str(e)[:60]}")

        print("\n=== facts: contradiction-relevant signals ===")
        # confirmation_count distribution + superseded chains
        n_super = await conn.fetchval(
            "SELECT count(*) FROM heart.facts WHERE agent_id=$1 AND superseded_by IS NOT NULL", AGENT)
        print(f"  facts with superseded_by set: {n_super}")
        # contradicts edges specifically
        n_contra = await conn.fetchval(
            "SELECT count(*) FROM brain.graph_edges WHERE agent_id=$1 AND relation='contradicts'", AGENT)
        print(f"  contradicts edges: {n_contra}")
        n_cof = await conn.fetchval(
            "SELECT count(*) FROM heart.facts WHERE agent_id=$1 AND contradiction_of IS NOT NULL", AGENT)
        print(f"  facts with contradiction_of set: {n_cof}")

        print("\n=== events: contradiction / supersession activity (last 90d) ===")
        rows = await conn.fetch(
            "SELECT event_type, count(*) n FROM nous_system.events "
            "WHERE agent_id=$1 AND (event_type ILIKE '%contrad%' OR event_type ILIKE '%supersed%' "
            "OR event_type ILIKE '%dedup%' OR event_type ILIKE '%link%') "
            "GROUP BY event_type ORDER BY n DESC LIMIT 30", AGENT)
        if not rows:
            print("  (no matching events)")
        for r in rows:
            print(f"  {r['event_type']:<40} {r['n']:>7}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
