"""F070 chunk-edge backfill (prod-safe).

Iteratively densifies orphan chunks until either every chunk has at least
one ``brain.graph_edges`` row (chunk -> *) or ``--max-batches`` is hit.
Idempotent: ``find_orphans`` excludes any chunk that already has an edge,
so re-running this script after a partial run continues from where the
last batch stopped.

Prereqs (in order):
  1. PR #448 is merged and the nous service has restarted at least once
     so ``Settings()`` picks up the new fields.
  2. Migration ``sql/migrations/051_f070_chunk_graph_edges.sql`` is
     applied (extends ``ck_edges_{source_type,target_type,relation}``).
  3. The agent corpus has ``heart.episode_chunks`` rows produced by F067
     (``NOUS_EPISODE_CHUNKS_ENABLED=true`` during session end). If chunks
     are absent, this script does nothing.

Usage::

    # one-shot, prod nous (read DB from env or .env)
    NOUS_CHUNK_CONSOLIDATION_ENABLED=true \
        uv run python scripts/backfill_f070_chunks.py \
        --agent-id nous-default

    # dry run — count orphans only, no writes
    uv run python scripts/backfill_f070_chunks.py \
        --agent-id nous-default --dry-run

    # cap the run (useful for first prod attempt)
    uv run python scripts/backfill_f070_chunks.py \
        --agent-id nous-default --max-batches 5 --batch-size 500

What it writes (per orphan chunk):
  * chunk -> episode  relation=part_of      (always, structural anchor)
  * chunk -> fact     relation=summarized_by  (same-episode, cosine ≥ threshold)
  * chunk -> chunk    relation=related_to     (sequential adjacency + intra-episode cosine ≥ threshold)

Empirical (eval DB, 35,768 orphans, BATCH=2000): ~30 sec per batch,
~6 edges per chunk on average, ~9 min end-to-end. Prod corpus is
smaller; expect proportionally less.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from sqlalchemy import text

from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger("f070-backfill")


async def _count_orphan_chunks(db: Database, agent_id: str) -> int:
    """Count chunks for the given agent that have NO graph edges yet.

    Mirrors ``GraphDensifier.find_orphans('chunk', ..., require_embedding=False)``
    so the count + the densifier's selection stay consistent.
    """
    async with db.engine.begin() as conn:
        r = await conn.execute(
            text(
                "SELECT COUNT(*) FROM heart.episode_chunks t "
                "WHERE t.agent_id = :a AND NOT EXISTS ("
                "  SELECT 1 FROM brain.graph_edges e "
                "  WHERE e.agent_id = :a AND ("
                "    (e.source_id = t.id AND e.source_type = 'chunk')"
                "    OR (e.target_id = t.id AND e.target_type = 'chunk')"
                "  )"
                ")"
            ),
            {"a": agent_id},
        )
        return int(r.scalar() or 0)


async def _summarize_chunk_edges(db: Database, agent_id: str) -> dict[str, int]:
    """Return {relation_label: count} for every chunk edge belonging to
    the agent. Used for the post-run summary print."""
    async with db.engine.begin() as conn:
        rows = (await conn.execute(
            text(
                "SELECT source_type || ' -> ' || target_type || ' [' "
                "       || relation || ']' AS label, COUNT(*) AS cnt "
                "FROM brain.graph_edges "
                "WHERE agent_id = :a "
                "  AND (source_type = 'chunk' OR target_type = 'chunk') "
                "GROUP BY label ORDER BY cnt DESC"
            ),
            {"a": agent_id},
        )).all()
    return {row.label: int(row.cnt) for row in rows}


async def run_backfill(
    *, agent_id: str, batch_size: int, max_batches: int | None,
    dry_run: bool,
) -> int:
    settings = Settings()

    if not settings.chunk_consolidation_enabled:
        print(
            "WARN: NOUS_CHUNK_CONSOLIDATION_ENABLED is False. "
            "GraphDensifier.backfill_orphan_chunks() short-circuits to 0 "
            "edges in this mode. Set the env var to 'true' before running.",
            file=sys.stderr,
        )
        return 1
    if not settings.graph_backfill_enabled:
        print(
            "WARN: NOUS_GRAPH_BACKFILL_ENABLED is False. Same problem — "
            "the densifier short-circuits without it. Set both flags.",
            file=sys.stderr,
        )
        return 1

    # Override the densifier's class-level cap with the CLI batch size so
    # each call processes the requested number of orphans even when the
    # operator left NOUS_GRAPH_BACKFILL_MAX_CHUNKS at its default.
    effective_batch = batch_size

    db = Database(settings)
    await db.connect()
    try:
        before = await _count_orphan_chunks(db, agent_id)
        print(
            f"Agent {agent_id}: {before} orphan chunks "
            f"(batch_size={effective_batch}, max_batches="
            f"{max_batches if max_batches is not None else 'unbounded'})"
        )
        if dry_run:
            print("--dry-run set; not writing edges. Exiting.")
            return 0
        if before == 0:
            print("Nothing to backfill — every chunk already has at least "
                  "one graph edge.")
            return 0

        embedder = EmbeddingProvider(settings)
        linker = GraphLinker(
            db=db, embedder=embedder, settings=settings, agent_id=agent_id,
        )
        densifier = GraphDensifier(
            db=db, graph_linker=linker, embedder=embedder,
            settings=settings, agent_id=agent_id,
        )

        total_edges = 0
        batch_n = 0
        start = time.time()
        while True:
            if max_batches is not None and batch_n >= max_batches:
                print(f"--max-batches={max_batches} hit; stopping.")
                break
            orphans_before = await _count_orphan_chunks(db, agent_id)
            if orphans_before == 0:
                break
            batch_n += 1
            t0 = time.time()
            created = await densifier.backfill_orphan_chunks(
                max_count=effective_batch,
            )
            dt = time.time() - t0
            total_edges += created
            orphans_after = await _count_orphan_chunks(db, agent_id)
            processed = orphans_before - orphans_after
            elapsed = (time.time() - start) / 60.0
            print(
                f"batch {batch_n:3d}: orphans {orphans_before:>7d} -> "
                f"{orphans_after:>7d}  (-{processed:>5d})  "
                f"+{created:>6d} edges in {dt:5.1f}s  "
                f"[total {total_edges:>7d} edges, {elapsed:.1f} min]",
                flush=True,
            )
            if created == 0 and processed == 0:
                print(
                    "WARN: batch made no progress (0 edges, 0 orphans "
                    "consumed). Stopping to avoid an infinite loop.",
                    file=sys.stderr,
                )
                break

        breakdown = await _summarize_chunk_edges(db, agent_id)
        print()
        print("Final chunk-edge breakdown:")
        for label, cnt in breakdown.items():
            print(f"  {label:<40} {cnt}")
        print(f"Total chunk edges for agent={agent_id}: {sum(breakdown.values())}")
        return 0
    finally:
        await db.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill F070 chunk graph edges for a single agent.",
    )
    parser.add_argument(
        "--agent-id", required=True,
        help="Agent identifier whose chunks to densify (e.g. nous-default).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=2000,
        help="Orphan chunks processed per call to backfill_orphan_chunks. "
             "Higher = fewer round-trips, lower = smoother memory. "
             "Eval default was 2000 / batch_dt≈30s.",
    )
    parser.add_argument(
        "--max-batches", type=int, default=None,
        help="Stop after this many batches even if orphans remain. "
             "Omit for unbounded. Useful for capping a first prod run.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the orphan count and exit without writing.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rc = asyncio.run(run_backfill(
        agent_id=args.agent_id,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        dry_run=args.dry_run,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
