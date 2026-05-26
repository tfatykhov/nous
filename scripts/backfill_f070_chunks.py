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


async def _count_chunks_lacking_cross_episode_edges(
    db: Database, agent_id: str,
) -> int:
    """F070.1: count chunks that have edges but no cross-episode ones.

    Mirrors the row-correlated NOT-EXISTS shape used in
    ``GraphDensifier.find_chunks_lacking_cross_episode_edges``. Codex
    round-2 P2: filters ``c.embedding IS NOT NULL`` because chunks
    without embeddings can't be cross-episode-linked and would otherwise
    inflate the count + occupy LIMIT slots indefinitely.
    """
    sql = text(
        """
        SELECT COUNT(*) FROM heart.episode_chunks c
        WHERE c.agent_id = :a
          AND c.embedding IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM brain.graph_edges e
              WHERE e.agent_id = :a
                AND ((e.source_id = c.id AND e.source_type = 'chunk')
                     OR (e.target_id = c.id AND e.target_type = 'chunk'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM brain.graph_edges e
              JOIN heart.facts f
                  ON e.target_id = f.id AND f.agent_id = :a
              WHERE e.agent_id = :a
                AND e.source_id = c.id
                AND e.source_type = 'chunk' AND e.target_type = 'fact'
                AND f.source_episode_id IS NOT NULL
                AND f.source_episode_id != c.episode_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM brain.graph_edges e
              JOIN heart.episode_chunks other
                  ON e.target_id = other.id AND other.agent_id = :a
              WHERE e.agent_id = :a
                AND e.source_id = c.id
                AND e.source_type = 'chunk' AND e.target_type = 'chunk'
                AND other.episode_id != c.episode_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM brain.graph_edges e
              JOIN heart.episode_chunks other
                  ON e.source_id = other.id AND other.agent_id = :a
              WHERE e.agent_id = :a
                AND e.target_id = c.id
                AND e.target_type = 'chunk' AND e.source_type = 'chunk'
                AND other.episode_id != c.episode_id
          )
        """
    )
    async with db.engine.begin() as conn:
        r = await conn.execute(sql, {"a": agent_id})
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
    dry_run: bool, mode: str = "same-episode",
) -> int:
    """Run the chunk-graph backfill.

    ``mode``:
      - ``same-episode`` — F070 v1 behavior; builds chunk→episode part_of,
        chunk→fact summarized_by (same-episode only), and chunk↔chunk
        related_to (sequential + intra-episode cosine).
      - ``cross-episode`` — F070.1 only; builds chunk→fact summarized_by
        ACROSS episodes and chunk↔chunk related_to ACROSS episodes.
      - ``all`` — run same-episode first, then cross-episode. Useful for
        a clean first deploy that hasn't had any backfill yet.
    """
    settings = Settings()

    if not settings.chunk_consolidation_enabled:
        print(
            "WARN: NOUS_CHUNK_CONSOLIDATION_ENABLED is False. "
            "Both backfill paths short-circuit to 0 edges in this mode. "
            "Set the env var to 'true' before running.",
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

    if mode not in ("same-episode", "cross-episode", "all"):
        print(f"ERROR: invalid --mode {mode!r}", file=sys.stderr)
        return 2

    # Override the densifier's class-level cap with the CLI batch size so
    # each call processes the requested number of orphans even when the
    # operator left NOUS_GRAPH_BACKFILL_MAX_CHUNKS at its default.
    effective_batch = batch_size

    db = Database(settings)
    await db.connect()
    try:
        # Dry-run shows BOTH counts so the operator knows where work lives.
        if dry_run:
            same_n = await _count_orphan_chunks(db, agent_id)
            cross_n = await _count_chunks_lacking_cross_episode_edges(
                db, agent_id,
            )
            print(
                f"Agent {agent_id} (mode={mode}):\n"
                f"  same-episode orphans:               {same_n}\n"
                f"  cross-episode candidates (existing): {cross_n}"
            )
            print("--dry-run set; not writing edges. Exiting.")
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
        start = time.time()

        # -----------------------------------------------------------
        # Phase 1: same-episode (F070 v1) — only when mode != cross-episode
        # -----------------------------------------------------------
        if mode in ("same-episode", "all"):
            before = await _count_orphan_chunks(db, agent_id)
            print(
                f"== same-episode phase ==\n"
                f"Agent {agent_id}: {before} orphan chunks "
                f"(batch_size={effective_batch}, max_batches="
                f"{max_batches if max_batches is not None else 'unbounded'})"
            )
            if before == 0:
                print("Nothing to backfill in same-episode phase.")
            else:
                batch_n = 0
                while True:
                    if max_batches is not None and batch_n >= max_batches:
                        print(f"--max-batches={max_batches} hit; stopping same-episode phase.")
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
                        f"  same-ep batch {batch_n:3d}: orphans "
                        f"{orphans_before:>7d} -> {orphans_after:>7d}  "
                        f"(-{processed:>5d})  +{created:>6d} edges in "
                        f"{dt:5.1f}s  [total {total_edges:>7d} edges, "
                        f"{elapsed:.1f} min]",
                        flush=True,
                    )
                    if created == 0 and processed == 0:
                        print(
                            "WARN: same-episode batch made no progress. "
                            "Stopping phase.",
                            file=sys.stderr,
                        )
                        break

        # -----------------------------------------------------------
        # Phase 2: cross-episode (F070.1) — only when mode != same-episode
        # -----------------------------------------------------------
        if mode in ("cross-episode", "all"):
            before = await _count_chunks_lacking_cross_episode_edges(db, agent_id)
            print(
                f"== cross-episode phase ==\n"
                f"Agent {agent_id}: {before} chunks lacking cross-episode "
                f"edges (batch_size={effective_batch}, max_batches="
                f"{max_batches if max_batches is not None else 'unbounded'})"
            )
            if before == 0:
                print("Nothing to backfill in cross-episode phase.")
            else:
                # Codex round-3 P1: track an ``attempted`` set across
                # batches and exclude those chunks from each subsequent
                # query. The earlier offset-based pagination had a bug:
                # when a batch successfully linked some chunks, those
                # chunks dropped out of the ordered result set, so
                # advancing offset by ``effective_batch`` jumped past
                # their (still-unlinked) neighbors. With exclusion
                # tracking, every chunk is visited at most once per
                # run regardless of per-batch success, and the loop
                # terminates when no un-attempted candidate remains.
                batch_n = 0
                attempted: set = set()
                while True:
                    if max_batches is not None and batch_n >= max_batches:
                        print(f"--max-batches={max_batches} hit; stopping cross-episode phase.")
                        break
                    candidates_before = await _count_chunks_lacking_cross_episode_edges(
                        db, agent_id,
                    )
                    if candidates_before == 0:
                        break
                    batch_n += 1
                    t0 = time.time()
                    created, attempted_ids = (
                        await densifier.backfill_orphan_chunks_cross_episode(
                            max_count=effective_batch,
                            exclude_ids=attempted,
                        )
                    )
                    if not attempted_ids:
                        # No un-attempted candidate remained — we've
                        # visited every linkable chunk this run.
                        print(
                            "Cross-episode phase: all candidates attempted "
                            "this run. Stopping. (If candidate count is "
                            "still > 0, those chunks were attempted and "
                            "either fell below "
                            "NOUS_GRAPH_THRESHOLD_CHUNK_FACT_CROSS / "
                            "NOUS_GRAPH_THRESHOLD_CHUNK_CHUNK_CROSS — "
                            "consider lowering thresholds and re-running.)"
                        )
                        break
                    attempted.update(attempted_ids)
                    dt = time.time() - t0
                    total_edges += created
                    candidates_after = await _count_chunks_lacking_cross_episode_edges(
                        db, agent_id,
                    )
                    processed = candidates_before - candidates_after
                    elapsed = (time.time() - start) / 60.0
                    print(
                        f"  cross-ep batch {batch_n:3d}: candidates "
                        f"{candidates_before:>7d} -> {candidates_after:>7d}  "
                        f"(-{processed:>5d})  +{created:>6d} edges in "
                        f"{dt:5.1f}s  attempted_set={len(attempted)}  "
                        f"[total {total_edges:>7d} edges, {elapsed:.1f} min]",
                        flush=True,
                    )

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
        help="Print the orphan count(s) and exit without writing.",
    )
    parser.add_argument(
        "--mode", default="same-episode",
        choices=["same-episode", "cross-episode", "all"],
        help=(
            "Which densification phase to run. "
            "'same-episode' (default) = F070 v1 behavior (chunk→episode "
            "part_of, chunk→fact same-episode, chunk↔chunk intra). "
            "'cross-episode' = F070.1 only (chunk→fact + chunk↔chunk "
            "ACROSS episodes; assumes same-episode has already been "
            "backfilled). 'all' = both phases in sequence."
        ),
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
        mode=args.mode,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
