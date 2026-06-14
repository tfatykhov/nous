"""Judge the current happened_before edges on the DB_*-selected DB (1 Sonnet call).

    DB_HOST=localhost DB_PORT=5433 DB_USER=nous DB_PASSWORD=nous_eval \\
    DB_NAME=nous_eval_prod AGENT=nous-default PYTHONPATH=. \\
    uv run python scripts/diag/judge_hb.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.edge_judge import judge_edges

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
AGENT = os.environ.get("AGENT", "nous-default")


async def main() -> None:
    s = Settings()
    db = Database(s)
    await db.connect()
    try:
        async with db.session() as session:
            rows = await session.execute(text(
                """
                SELECT e.source_id, e.target_id, a.content AS a_c, b.content AS b_c
                FROM brain.graph_edges e
                JOIN heart.facts a ON a.id = e.source_id
                JOIN heart.facts b ON b.id = e.target_id
                WHERE e.agent_id = :a AND e.relation = 'happened_before'
                """
            ), {"a": AGENT})
            edges = [
                {"source_id": str(r.source_id), "target_id": str(r.target_id),
                 "source_type": "fact", "target_type": "fact",
                 "relation": "happened_before",
                 "source_content": r.a_c, "target_content": r.b_c}
                for r in rows
            ]
    finally:
        await db.engine.dispose()

    if not edges:
        print("no happened_before edges")
        return
    verdicts = await judge_edges(edges, s)
    yes = sum(1 for v in verdicts if v.verdict == "YES")
    weak = sum(1 for v in verdicts if v.verdict == "WEAK")
    no = sum(1 for v in verdicts if v.verdict == "NO")
    denom = yes + weak + no
    print(f"n={len(verdicts)}  YES={yes} WEAK={weak} NO={no}  "
          f"precision={yes/denom if denom else 0:.2f}")
    for v in verdicts:
        print(f"  [{v.verdict}] {v.reasoning[:130]}")


if __name__ == "__main__":
    asyncio.run(main())
