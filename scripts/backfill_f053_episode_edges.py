"""F053 remediation — restore episode graph edges wrongly pruned.

Background (prod audit 2026-07-12): F053's dead-edge prune treated every
normally-closed episode (`active=false` = 008.3 lifecycle close) as a
dead node and deleted its incident edges nightly; F040 could not rebuild
them (`t.active = true` orphan filter). 657 closed prod episodes held 6
edges total; chunk→episode part_of was down to 3/3,060.

ORDERING IS LOAD-BEARING: the deterministic anchors de-orphan every
episode they touch (find_orphans counts ANY non-excluded incident edge),
so the cosine drain must run FIRST or F040 can never build semantic
episode↔episode edges for the historical population.

  Phase 1 (--densify, optional): drain the orphan-eligible closed
      episodes through the normal F040 backfill (episode↔episode
      related_to + episode→fact cross-type; embedding + optional CE
      cost) instead of waiting ~NOUS_GRAPH_BACKFILL_MAX_EPISODES per
      nightly sleep cycle. Skipping this phase permanently forgoes
      cosine healing for episodes the anchors then de-orphan.
  Phase 2 (always): deterministic re-anchor from FK ground truth via
      GraphDensifier.restore_episode_anchor_edges():
        * chunk   → episode  part_of         (weight 1.0, deterministic)
        * fact    → episode  extracted_from  (active facts, weight 1.0)
        * episode → decision discussed_in    (episode_decisions join table)

Run AFTER deploying the prune fix, or the next sleep cycle re-deletes
everything this restores. Pin NOUS_SPREADING_ACTIVATION_ENABLED=false
first (the ~5k restored edges count toward the auto-spreading density
gate; prod sits at 2.745 vs threshold 3.0 and spreading measured
negative on prod).

Usage:
    # counts only (both phases, computed before any write)
    uv run python scripts/backfill_f053_episode_edges.py \
        --agent-id nous-default --dry-run --densify

    # full remediation (recommended): drain, then anchor
    uv run python scripts/backfill_f053_episode_edges.py \
        --agent-id nous-default --densify --max-batches 30

    # anchors only (accepts forgoing cosine healing)
    uv run python scripts/backfill_f053_episode_edges.py \
        --agent-id nous-default
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger("f053-restore")


async def _count_orphan_episodes(db: Database, agent_id: str) -> int:
    """Count orphan-eligible episodes (mirrors find_orphans('episode') with
    the liveness extra_where; require_embedding matches the backfill's
    default True)."""
    from nous.brain.graph_constants import (
        autobehavior_exclusion_sql,
        episode_live_sql,
    )

    excl = autobehavior_exclusion_sql("e.")
    live = episode_live_sql("t.")
    async with db.engine.begin() as conn:
        r = await conn.execute(
            text(
                f"SELECT COUNT(*) FROM heart.episodes t "
                f"WHERE t.agent_id = :a AND {live} "
                f"  AND t.embedding IS NOT NULL "
                f"  AND NOT EXISTS ("
                f"    SELECT 1 FROM brain.graph_edges e "
                f"    WHERE e.agent_id = :a AND {excl} AND ("
                f"      (e.source_id = t.id AND e.source_type = 'episode')"
                f"      OR (e.target_id = t.id AND e.target_type = 'episode')"
                f"    )"
                f"  )"
            ),
            {"a": agent_id},
        )
        return int(r.scalar() or 0)


async def run(
    *, agent_id: str, dry_run: bool, densify: bool,
    max_batches: int | None,
) -> int:
    settings = Settings()

    db = Database(settings)
    await db.connect()
    try:
        embedder = EmbeddingProvider(settings)
        linker = GraphLinker(
            db=db, embedder=embedder, settings=settings, agent_id=agent_id,
        )
        densifier = GraphDensifier(
            db=db, graph_linker=linker, embedder=embedder,
            settings=settings, agent_id=agent_id,
        )

        run_started = datetime.now(UTC).isoformat()
        print(f"Run start (rollback key: created_at >= this): {run_started}")

        # Phase 1 — cosine drain (MUST precede the anchors: they de-orphan).
        if densify:
            # Gate applies to this phase only — the deterministic anchor
            # restore (Phase 2) does not depend on the backfill flag.
            if not settings.graph_backfill_enabled:
                print(
                    "WARN: NOUS_GRAPH_BACKFILL_ENABLED is False — "
                    "backfill_orphan_episodes short-circuits to 0. "
                    "Set the env var, or drop --densify.",
                    file=sys.stderr,
                )
                return 1
            if dry_run:
                n = await _count_orphan_episodes(db, agent_id)
                print(f"Phase 1 (dry-run): {n} orphan-eligible episodes")
            else:
                batch_n, total = 0, 0
                start = time.time()
                while True:
                    if max_batches is not None and batch_n >= max_batches:
                        print(f"--max-batches={max_batches} hit; stopping.")
                        break
                    before = await _count_orphan_episodes(db, agent_id)
                    if before == 0:
                        break
                    batch_n += 1
                    created = await densifier.backfill_orphan_episodes()
                    total += created
                    after = await _count_orphan_episodes(db, agent_id)
                    print(
                        f"  batch {batch_n}: {created} edges, "
                        f"orphans {before} -> {after}"
                    )
                    if after >= before:
                        # find_orphans is newest-first with a fixed LIMIT: a
                        # head of below-threshold orphans blocks the queue, so
                        # re-running will NOT drain the remainder. Honest stop.
                        print(
                            f"No orphan progress — {after} episodes have no "
                            f"above-threshold candidates (stuck head; re-runs "
                            f"won't help; they'll be anchored by Phase 2).",
                        )
                        break
                print(
                    f"Phase 1: {total} edges in {batch_n} batches "
                    f"({time.time() - start:.0f}s)"
                )

        # Phase 2 — deterministic anchors
        counts = await densifier.restore_episode_anchor_edges(dry_run=dry_run)
        label = "would restore" if dry_run else "restored"
        print(
            f"Phase 2 ({label}): part_of={counts['part_of']} "
            f"extracted_from={counts['extracted_from']} "
            f"discussed_in={counts['discussed_in']}"
        )
        return 0
    finally:
        await db.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--densify", action="store_true",
        help="Drain orphan-eligible episodes through F040 backfill BEFORE "
             "anchoring (embedding + optional CE cost). Recommended — "
             "skipping it forgoes cosine healing for the anchored episodes.",
    )
    parser.add_argument(
        "--max-batches", type=int, default=None,
        help="Cap on --densify batches (default: run until drained).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(run(
        agent_id=args.agent_id, dry_run=args.dry_run,
        densify=args.densify, max_batches=args.max_batches,
    ))


if __name__ == "__main__":
    sys.exit(main())
