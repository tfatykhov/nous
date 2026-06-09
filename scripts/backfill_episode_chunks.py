"""One-shot F067 backfill: chunk + embed transcripts for episodes that
don't yet have chunks.

When ``NOUS_EPISODE_CHUNKS_ENABLED`` is flipped on in an existing
deployment, only NEW episodes get chunked — the ingest hook fires inside
``EpisodeSummarizer.summarize_episode`` which early-returns for already-
summarized episodes. This script processes the backlog.

It uses the same chunking + embedding path as the live hook
(``_chunk_and_store_transcript``):

- ``chunk_text()`` with ``settings.episode_chunk_size`` / ``overlap`` /
  ``min_transcript_chars``
- ``EmbeddingProvider.embed_batch`` for batched embedding
- ``INSERT ... ON CONFLICT (episode_id, chunk_index) DO NOTHING`` for
  idempotency (matches the unique index on heart.episode_chunks)

Failure semantics match the live path: when ``embed_batch`` fails for an
episode, the script ABORTS that episode's chunk insert (no NULL-embedding
rows persisted) and increments ``embed_failures``. A later re-run will
retry the same episode because NOT EXISTS still matches.

Usage:
    # Dry-run / count
    uv run python scripts/backfill_episode_chunks.py --dry-run

    # Single agent
    uv run python scripts/backfill_episode_chunks.py --agent-id nous-default

    # All agents
    uv run python scripts/backfill_episode_chunks.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chunk_backfill")

EPISODE_BATCH = 50  # How many episodes to claim per outer loop iteration.


@dataclass
class Stats:
    episodes_seen: int = 0
    episodes_processed: int = 0
    episodes_skipped_short: int = 0
    chunks_written: int = 0
    embed_failures: int = 0


async def _next_batch(db, agent_id: str | None, limit: int) -> list[tuple[str, str, str]]:
    """Return up to ``limit`` (episode_id, agent_id, transcript) tuples
    for episodes that have a non-null transcript and no existing chunks.

    NOT EXISTS naturally skips episodes whose chunks were just inserted by
    the previous iteration, so the outer loop can simply re-query until
    this returns empty.
    """
    from sqlalchemy import text as sa_text
    where = "e.transcript IS NOT NULL AND length(trim(e.transcript)) > 0"
    params: dict = {"limit": limit}
    if agent_id is not None:
        where += " AND e.agent_id = :a"
        params["a"] = agent_id
    async with db.session() as s:
        rows = (await s.execute(sa_text(
            f"SELECT e.id::text, e.agent_id, e.transcript "
            f"FROM heart.episodes e "
            f"WHERE {where} "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM heart.episode_chunks ec WHERE ec.episode_id = e.id"
            f"      AND ec.source_kind = 'dialogue'"
            f"  ) "
            f"ORDER BY e.created_at "
            f"LIMIT :limit"
        ), params)).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def _process_episode(
    db, embedder, settings,
    episode_id: str, agent_id: str, transcript: str,
    stats: Stats,
) -> None:
    """Chunk + embed + insert for one episode. Mirrors the failure
    semantics of ``_chunk_and_store_transcript`` — abort on embed
    failure rather than persist NULL-embedding rows."""
    from sqlalchemy import text as sa_text
    from nous.heart.chunking import chunk_text

    chunks = chunk_text(
        transcript,
        chunk_size=settings.episode_chunk_size,
        overlap=settings.episode_chunk_overlap,
        min_chars=settings.episode_chunk_min_transcript_chars,
    )
    if not chunks:
        stats.episodes_skipped_short += 1
        return

    try:
        vectors = await embedder.embed_batch(chunks)
    except Exception:
        logger.warning(
            "embed_batch failed for episode %s (agent_id=%s, %d chunks) — "
            "skipping; will retry on next run",
            episode_id, agent_id, len(chunks), exc_info=True,
        )
        stats.embed_failures += 1
        return
    if len(vectors) != len(chunks):
        logger.warning(
            "embedder returned %d vectors for %d chunks (episode %s); skipping",
            len(vectors), len(chunks), episode_id,
        )
        stats.embed_failures += 1
        return

    written = 0
    async with db.session() as s:
        # Audit E1 parity (2026-06-09): serialize with ingest_document /
        # the live summarizer via the same episode-scoped advisory lock and
        # allocate from MAX+1 — a concurrent ingest_document on the same
        # episode otherwise collides on (episode_id, chunk_index) and
        # DO NOTHING silently destroys rows. Also re-check the dialogue-
        # chunk existence under the lock (the batch query ran lock-free).
        await s.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"ingest_document:{episode_id}"},
        )
        already = await s.execute(sa_text(
            "SELECT 1 FROM heart.episode_chunks "
            "WHERE agent_id = :agent_id AND episode_id = :ep "
            "  AND source_kind = 'dialogue' LIMIT 1"
        ), {"agent_id": agent_id, "ep": episode_id})
        if already.first() is not None:
            await s.commit()  # release the advisory lock
            stats.episodes_processed += 1
            return
        next_idx = int((await s.execute(sa_text(
            "SELECT COALESCE(MAX(chunk_index), -1) + 1 "
            "FROM heart.episode_chunks "
            "WHERE agent_id = :agent_id AND episode_id = :ep"
        ), {"agent_id": agent_id, "ep": episode_id})).scalar() or 0)
        for offset, (content, vec) in enumerate(zip(chunks, vectors)):
            if not vec:
                continue
            vec_lit = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
            await s.execute(sa_text(
                "INSERT INTO heart.episode_chunks "
                "(agent_id, episode_id, chunk_index, content, embedding) "
                "VALUES (:agent_id, :ep, :idx, :content, CAST(:vec AS vector)) "
                "ON CONFLICT (episode_id, chunk_index) DO NOTHING"
            ), {
                "agent_id": agent_id, "ep": episode_id,
                "idx": next_idx + offset, "content": content, "vec": vec_lit,
            })
            written += 1
        await s.commit()
    stats.episodes_processed += 1
    stats.chunks_written += written


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent-id", default=None,
        help="Process only this agent_id. Default: every agent in heart.episodes.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count eligible episodes (transcript IS NOT NULL, no chunks yet); no API calls.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Cap total episodes processed (0 = all). Useful for cost-bounded smoke runs.",
    )
    args = parser.parse_args()

    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.storage.database import Database
    from sqlalchemy import text as sa_text

    settings = Settings()
    db = Database(settings)
    await db.connect()

    # Pre-flight: count what's eligible
    where = "e.transcript IS NOT NULL AND length(trim(e.transcript)) > 0"
    pre_params: dict = {}
    if args.agent_id:
        where += " AND e.agent_id = :a"
        pre_params["a"] = args.agent_id
    async with db.session() as s:
        eligible = (await s.execute(sa_text(
            f"SELECT COUNT(*) FROM heart.episodes e "
            f"WHERE {where} AND NOT EXISTS ("
            f"  SELECT 1 FROM heart.episode_chunks ec WHERE ec.episode_id = e.id"
            f")"
        ), pre_params)).scalar()

    print()
    print("=" * 60)
    print("F067 episode-chunk backfill")
    print("=" * 60)
    print(f"  Eligible episodes:   {eligible}")
    print(f"  Agent filter:        {args.agent_id or '(all agents)'}")
    print(f"  Chunk size / overlap: {settings.episode_chunk_size} / {settings.episode_chunk_overlap}")
    print(f"  Min transcript chars: {settings.episode_chunk_min_transcript_chars}")
    print(f"  Embedder:            {settings.embedding_model} @ {settings.embedding_dimensions}d")
    print(f"  Dry run:             {args.dry_run}")
    if args.limit:
        print(f"  Limit:               {args.limit}")
    print()

    if args.dry_run or eligible == 0:
        await db.disconnect()
        return 0

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY required for embedding")

    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    stats = Stats()

    try:
        # codex P1 (PR #495): episodes that fail without inserting a chunk
        # (too-short transcript, repeated embed failure, vector-count
        # mismatch) stay eligible and are re-selected by every batch —
        # without --limit one bad episode loops the run forever and an
        # embed failure retries the API unbounded. Attempt each episode at
        # most once per run; cross-run retry semantics are unchanged.
        attempted: set[str] = set()
        while True:
            if args.limit and stats.episodes_seen >= args.limit:
                break
            remaining_budget = (
                args.limit - stats.episodes_seen if args.limit else EPISODE_BATCH
            )
            batch_size = min(EPISODE_BATCH, remaining_budget)
            batch = await _next_batch(db, args.agent_id, batch_size)
            fresh = [(e, a, t) for e, a, t in batch if e not in attempted]
            if not fresh:
                if batch:
                    logger.info(
                        "Stopping: remaining %d eligible episode(s) already "
                        "attempted this run (see failures above)", len(batch),
                    )
                break
            for episode_id, agent_id, transcript in fresh:
                attempted.add(episode_id)
                stats.episodes_seen += 1
                await _process_episode(
                    db, embedder, settings,
                    episode_id, agent_id, transcript,
                    stats,
                )
            logger.info(
                "Progress: seen=%d processed=%d short=%d failures=%d chunks=%d",
                stats.episodes_seen, stats.episodes_processed,
                stats.episodes_skipped_short, stats.embed_failures,
                stats.chunks_written,
            )
    finally:
        await embedder.close()
        await db.disconnect()

    print()
    print("=" * 60)
    print("Backfill complete")
    print("=" * 60)
    print(f"  episodes_processed:        {stats.episodes_processed}")
    print(f"  episodes_skipped_short:    {stats.episodes_skipped_short} (transcript < min_chars)")
    print(f"  chunks_written:            {stats.chunks_written}")
    print(f"  embed_failures:            {stats.embed_failures}")
    print(f"  episodes_seen_total:       {stats.episodes_seen}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
