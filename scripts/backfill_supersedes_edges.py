"""One-time backfill: write the ``supersedes`` graph edge for every fact whose
``superseded_by`` column is set but has no corresponding edge.

Context (2026-06-13 prod audit): prod had 261 facts with ``superseded_by``
populated but only 2 ``supersedes`` edges. The dominant supersession path
``FactManager._supersede_by_subject`` (and the sleep-cycle MERGE path) set the
column without mirroring it into ``brain.graph_edges``, so 259 supersessions
were invisible to the graph layer (densifier, adjacency boost, dashboards).
The code fix writes the edge going forward; this script retrofits history.

The edge direction mirrors the live writers (facts.py ``_apply_band_action`` /
``_supersede_by_subject``): ``source = superseding fact`` (the ``superseded_by``
target), ``target = superseded fact``. ``extraction_method='deterministic'``
matches ``edge_provenance.classify('supersedes')``.

Idempotent: ``ON CONFLICT (source_id, target_id, relation) DO NOTHING`` (column
list, not the constraint name — prod's unique index is auto-named, see
``backfill_auto_link_decisions.py``). Safe to re-run.

Usage::

    # dry run — count missing edges only
    uv run python scripts/backfill_supersedes_edges.py --agent-id nous-default --dry-run

    # full backfill (prod)
    uv run python scripts/backfill_supersedes_edges.py --agent-id nous-default
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database

# Facts whose superseded_by points at a real (still-present) fact but which
# have no supersedes edge yet. Self-supersession (id == superseded_by) is
# excluded defensively — it should never exist but would be a degenerate edge.
_MISSING_SQL = """
SELECT count(*)
FROM heart.facts f
JOIN heart.facts s ON s.id = f.superseded_by AND s.agent_id = f.agent_id
WHERE f.agent_id = :a
  AND f.superseded_by IS NOT NULL
  AND f.id <> f.superseded_by
  AND NOT EXISTS (
      SELECT 1 FROM brain.graph_edges e
      WHERE e.source_id = f.superseded_by
        AND e.target_id = f.id
        AND e.relation = 'supersedes'
  )
"""

_INSERT_SQL = """
INSERT INTO brain.graph_edges
    (source_id, target_id, source_type, target_type, agent_id,
     relation, weight, auto_linked, extraction_method)
SELECT f.superseded_by, f.id, 'fact', 'fact', f.agent_id,
       'supersedes', 1.0, true, 'deterministic'
FROM heart.facts f
JOIN heart.facts s ON s.id = f.superseded_by AND s.agent_id = f.agent_id
WHERE f.agent_id = :a
  AND f.superseded_by IS NOT NULL
  AND f.id <> f.superseded_by
ON CONFLICT (source_id, target_id, relation) DO NOTHING
"""


async def run_backfill(*, agent_id: str, dry_run: bool) -> int:
    settings = Settings()
    db = Database(settings)
    await db.connect()
    try:
        async with db.engine.begin() as conn:
            missing = int(
                (await conn.execute(text(_MISSING_SQL), {"a": agent_id})).scalar() or 0
            )
            print(f"Agent {agent_id}: {missing} superseded facts missing a supersedes edge.")
            if dry_run:
                print("--dry-run set; not writing edges. Exiting.")
                return 0
            if missing == 0:
                print("Nothing to backfill.")
                return 0
            result = await conn.execute(text(_INSERT_SQL), {"a": agent_id})
            inserted = result.rowcount
        print(f"Done. Inserted {inserted} supersedes edges for {agent_id}.")
        return 0
    finally:
        await db.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time supersedes-edge retrofit for facts with superseded_by set.",
    )
    parser.add_argument("--agent-id", required=True, help="Agent whose facts to retrofit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print missing-edge count and exit without writing.")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_backfill(agent_id=args.agent_id, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
