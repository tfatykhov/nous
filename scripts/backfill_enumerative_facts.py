#!/usr/bin/env python
"""064 R1.4: backfill enumerative facts from stored episode transcripts.

Conventions (#557): --dry-run counts first; prints a rollback key BEFORE any
write; --agent-id scoping; idempotent (Leg-2 native-cosine dedup at 0.95 makes
re-runs safe; already-extracted episodes converge to dedup-skips).

ROLLBACK: all facts written by this script have source='enumerative_extractor'.
    UPDATE heart.facts SET active=false
    WHERE agent_id=:a AND source='enumerative_extractor' AND created_at >= :watermark;
(never hard-delete; reactivation is the inverse.)
"""
# argparse: --agent-id (required), --dry-run, --max-episodes (default 0 = all),
# --since (ISO date, default None), --density-threshold (override),
# --extraction-budget (int, default 0 = unlimited)
#
# main():
#   settings = Settings()  # then force the R1 knobs on for this process:
#   settings = settings.model_copy(update={
#       "extraction_enumerative_enabled": True,
#       "enumerative_density_threshold": args.density_threshold or settings.enumerative_density_threshold,
#       "enumerative_extraction_max_per_hour": args.extraction_budget,  # 0 = unlimited
#   })
#   watermark = datetime.now(UTC).isoformat()
#   print(f"ROLLBACK KEY (created_at watermark): {watermark}")
#   episodes = await select_backfill_episodes(...)  # id + transcript, oldest first
#   if args.dry_run:
#       enumerable = [e for e in episodes if is_enumerable(e.transcript, thr)]
#       print(f"DRY RUN: {len(episodes)} episodes with transcripts, "
#             f"{len(enumerable)} classified enumerable — no writes.")
#       return
#   for ep in episodes:
#       ids = await extractor.process_transcript(ep.transcript, ep.id)
#       total += len(ids)
#   print(f"Backfilled {total} enumerative facts across {len(episodes)} episodes.")
#   (budget note: the extractor's hourly caps read from this process's Settings
#   copy — the script accepts --extraction-budget N mirroring Task 13's
#   --classifier-budget, default 0 = unlimited for offline clone remediation)
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, date, datetime

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger("enumerative-backfill")


async def select_backfill_episodes(session, agent_id: str, since, limit: int):
    """Return episodes with non-empty transcript, oldest first (id + transcript).

    Args:
        session: SQLAlchemy AsyncSession.
        agent_id: filter by agent.
        since: datetime.date or None — only episodes started on/after this date.
        limit: max episodes to return (0 = all).

    Returns:
        List of Row objects with .id and .transcript attributes.
    """
    params: dict = {"agent_id": agent_id}
    since_clause = ""
    if since is not None:
        since_clause = "AND started_at >= :since"
        params["since"] = since
    limit_clause = ""
    if limit > 0:
        limit_clause = "LIMIT :limit"
        params["limit"] = limit

    result = await session.execute(
        text(
            f"""
            SELECT id, transcript
            FROM heart.episodes
            WHERE agent_id = :agent_id
              AND active = true
              AND transcript IS NOT NULL
              AND length(transcript) > 0
              {since_clause}
            ORDER BY started_at ASC
            {limit_clause}
            """
        ),
        params,
    )
    return result.all()


async def _run_backfill(
    *,
    agent_id: str,
    since,
    max_episodes: int,
    density_threshold,
    extraction_budget: int,
    dry_run: bool,
) -> int:
    from nous.handlers.enumerative_extractor import EnumerativeExtractor, is_enumerable

    settings = Settings()

    # Live mode requires an Anthropic key — fail fast BEFORE the watermark so we
    # don't print a watermark that corresponds to zero writes.
    if not dry_run and not (
        settings.anthropic_api_key or getattr(settings, "anthropic_auth_token", None)
    ):
        print(
            "ERROR: Anthropic API key required for live mode "
            "(ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN).",
            file=sys.stderr,
        )
        return 2

    # Force R1 knobs on for this process.
    settings = settings.model_copy(update={
        "extraction_enumerative_enabled": True,
        "enumerative_density_threshold": (
            density_threshold or settings.enumerative_density_threshold
        ),
        "enumerative_extraction_max_per_hour": extraction_budget,  # 0 = unlimited
    })
    thr = settings.enumerative_density_threshold

    watermark = datetime.now(UTC).isoformat()
    print(f"ROLLBACK KEY (created_at watermark): {watermark}")

    db = Database(settings)
    await db.connect()
    try:
        async with db.session() as session:
            episodes = await select_backfill_episodes(
                session, agent_id, since, max_episodes
            )

        if dry_run:
            enumerable = [e for e in episodes if is_enumerable(e.transcript, thr)]
            print(
                f"DRY RUN: {len(episodes)} episodes with transcripts, "
                f"{len(enumerable)} classified enumerable — no writes."
            )
            return 0

        # Live run: construct Heart + EnumerativeExtractor + LLM client.
        from nous.api.anthropic_client import create_client
        from nous.brain.embeddings import EmbeddingProvider
        from nous.heart.heart import Heart

        embedder = None
        if settings.openai_api_key:
            embedder = EmbeddingProvider(
                api_key=settings.openai_api_key,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                cache_size=settings.embedding_cache_size,
            )

        heart = Heart(
            database=db,
            settings=settings,
            embedding_provider=embedder,
            owns_embeddings=False,
        )
        client = create_client(settings)
        await client.start()
        try:
            async with heart:
                extractor = EnumerativeExtractor(
                    heart=heart,
                    settings=settings,
                    llm_client=client,
                    embedder=embedder,
                )
                total = 0
                for ep in episodes:
                    ids = await extractor.process_transcript(ep.transcript, ep.id)
                    total += len(ids)
                print(
                    f"Backfilled {total} enumerative facts "
                    f"across {len(episodes)} episodes."
                )
        finally:
            await client.close()

        return 0
    except Exception:
        logger.exception("Backfill failed")
        return 2
    finally:
        await db.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="064 R1.4: backfill enumerative facts from stored episode transcripts.",
    )
    parser.add_argument(
        "--agent-id", required=True,
        help="Agent identifier (e.g. nous-default).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count episodes + classify; print counts; no writes.",
    )
    parser.add_argument(
        "--max-episodes", type=int, default=0,
        help="Max episodes to process (0 = all).",
    )
    parser.add_argument(
        "--since", default=None,
        help="ISO date (YYYY-MM-DD); only episodes started on or after this date.",
    )
    parser.add_argument(
        "--density-threshold", type=float, default=None,
        help="Override enumerative density threshold (0.0-1.0).",
    )
    parser.add_argument(
        "--extraction-budget", type=int, default=0,
        help="Hourly extraction LLM call cap (0 = unlimited; "
             "maps to enumerative_extraction_max_per_hour).",
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

    since = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(
                f"ERROR: --since {args.since!r} is not a valid ISO date (YYYY-MM-DD).",
                file=sys.stderr,
            )
            sys.exit(2)

    sys.exit(asyncio.run(_run_backfill(
        agent_id=args.agent_id,
        since=since,
        max_episodes=args.max_episodes,
        density_threshold=args.density_threshold,
        extraction_budget=args.extraction_budget,
        dry_run=args.dry_run,
    )))


if __name__ == "__main__":
    main()
