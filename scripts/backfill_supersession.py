#!/usr/bin/env python
"""064 R2.5/R2.6: backfill supersession — resolve same-key fact conflicts in the DB.

Finds pairs of active facts sharing (subject_key, attribute_key) and routes them
through the F027 classifier to determine which fact is current. Designed for
offline clone remediation where the live write-time key-conflict detection (R2.1)
was disabled or unavailable.

CLASSIFIER BUDGET WARNING
At the live default of 500 calls/hour, a run resolving hundreds of chains stalls
for hours. Pass --classifier-budget 0 (the default) to run in UNLIMITED mode —
the intended configuration for offline/clone remediation. The in-process counter
is always fresh per process, so the prod environment's live hourly cap is
unaffected.

ROLLBACK SQL
    -- Undo all supersessions written by this backfill (replace :a and :w):
    UPDATE heart.facts
       SET superseded_by = NULL, active = true
     WHERE agent_id = :a
       AND superseded_by IS NOT NULL
       AND updated_at >= :w;
    -- Remove supersedes edges created by this backfill:
    DELETE FROM brain.graph_edges
     WHERE relation = 'supersedes'
       AND created_at >= :w;
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger("supersession-backfill")


async def run_sweep(
    heart,
    settings,
    *,
    max_pairs: int,
    batch_size: int,
    dry_run: bool,
) -> dict:
    """Core sweep loop: find and (optionally) resolve same-key conflict pairs.

    Applies *settings* to heart.facts so the forced budget/flag take effect.
    Returns a dict with keys:
        pairs_examined      — distinct pairs seen this run
        resolutions_written — pairs where classify+policy produced a supersession
        keep_both           — pairs where resolve returned False
        sample_resolutions  — first 10 (c1, c2) texts from successful resolves

    Termination invariants:
    - KEEP-BOTH pairs remain active in the DB and re-appear in subsequent
      find_key_conflict_pairs calls. The seen frozenset prevents re-counting.
    - The fetch limit grows as len(seen) + batch_size so dry-run (nothing
      disappears) iterates through all distinct pairs without an infinite loop.
    - Stop when find_key_conflict_pairs returns no new pairs.
    """
    # Apply forced settings to FactManager for this run.
    heart.facts._settings = settings

    counters: dict = {
        "pairs_examined": 0,
        "resolutions_written": 0,
        "keep_both": 0,
        "sample_resolutions": [],  # list[tuple[str, str]]
    }
    seen: set[frozenset] = set()

    while True:
        if max_pairs > 0 and counters["pairs_examined"] >= max_pairs:
            break

        # Expand the window so unseen pairs appear beyond previously-seen ones.
        fetch_limit = len(seen) + batch_size
        pairs = await heart.facts.find_key_conflict_pairs(limit=fetch_limit)

        if not pairs:
            break

        new_pairs = [p for p in pairs if frozenset({p["id1"], p["id2"]}) not in seen]
        if not new_pairs:
            # All pairs in this batch already processed — stable state, stop.
            break

        for p in new_pairs:
            if max_pairs > 0 and counters["pairs_examined"] >= max_pairs:
                break

            id1: UUID = p["id1"]
            id2: UUID = p["id2"]
            c1: str = p["c1"]
            c2: str = p["c2"]

            seen.add(frozenset({id1, id2}))
            counters["pairs_examined"] += 1

            if dry_run:
                continue

            resolved = await heart.facts.resolve_key_conflict_pair(id1, id2, c1, c2)
            if resolved:
                counters["resolutions_written"] += 1
                if len(counters["sample_resolutions"]) < 10:
                    counters["sample_resolutions"].append((c1, c2))
            else:
                counters["keep_both"] += 1

    return counters


async def _chain_depth_histogram(session, agent_id: str, watermark: str) -> Counter:
    """Return a Counter of chain depths for winners touched this run.

    Uses the DB trigger-updated updated_at to identify losers written in this
    run, then walks each winner's downstream chain recursively.
    """
    # Find distinct winners from this run.
    winner_sql = text("""
        SELECT DISTINCT superseded_by AS winner_id
        FROM heart.facts
        WHERE agent_id = :agent_id
          AND superseded_by IS NOT NULL
          AND updated_at >= :watermark
    """)
    rows = await session.execute(winner_sql, {"agent_id": agent_id, "watermark": watermark})
    winner_ids = [r.winner_id for r in rows.fetchall() if r.winner_id is not None]

    if not winner_ids:
        return Counter()

    histogram: Counter = Counter()
    depth_sql = text("""
        WITH RECURSIVE downstream AS (
            SELECT id, 1 AS depth
            FROM heart.facts
            WHERE superseded_by = :winner_id
              AND agent_id = :agent_id
              AND active = false
            UNION ALL
            SELECT f.id, d.depth + 1
            FROM heart.facts f
            JOIN downstream d ON f.superseded_by = d.id
            WHERE d.depth < 20
              AND f.agent_id = :agent_id
        )
        SELECT COALESCE(MAX(depth), 0) AS depth FROM downstream
    """)
    for wid in winner_ids:
        r = await session.execute(depth_sql, {"winner_id": wid, "agent_id": agent_id})
        depth = r.scalar() or 0
        histogram[depth] += 1

    return histogram


async def _run_backfill(
    *,
    agent_id: str,
    max_pairs: int,
    batch_size: int,
    classifier_budget: int,
    dry_run: bool,
) -> int:
    settings = Settings()

    # Live mode requires the Anthropic key — fail fast BEFORE watermark so we
    # don't print a rollback key that corresponds to zero writes.
    if not dry_run and not (
        settings.anthropic_api_key or getattr(settings, "anthropic_auth_token", None)
    ):
        print(
            "ERROR: Anthropic API key required for live mode "
            "(ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN).",
            file=sys.stderr,
        )
        return 2

    # Force R2 knobs on for this process.
    settings = settings.model_copy(update={
        "supersession_key_resolution_enabled": True,
        "supersession_classifier_max_per_hour": classifier_budget,  # 0 = unlimited
    })

    watermark = datetime.now(UTC).isoformat()
    print(f"ROLLBACK KEY (updated_at watermark): {watermark}")

    db = Database(settings)
    await db.connect()
    try:
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
        # Override agent_id from CLI arg.
        heart.facts.agent_id = agent_id

        if not dry_run:
            from nous.api.anthropic_client import create_client
            client = create_client(settings)
            await client.start()
            try:
                async with heart:
                    heart.facts.set_llm_client(client, settings.background_model)
                    counters = await run_sweep(
                        heart,
                        settings,
                        max_pairs=max_pairs,
                        batch_size=batch_size,
                        dry_run=False,
                    )
                    _print_report(counters, heart, agent_id, watermark, dry_run=False)

                    # Chain histogram after sweep (same session context).
                    if counters["resolutions_written"] > 0:
                        async with db.session() as s:
                            hist = await _chain_depth_histogram(s, agent_id, watermark)
                        _print_chain_histogram(hist)
                    # Budget note.
                    if not heart.facts._key_budget_ok():
                        print(
                            "\nNOTE: classifier budget exhausted mid-run — "
                            "some keep_both returns may be budget-gated, not genuine KEEP-BOTH."
                        )
            finally:
                await client.close()
        else:
            async with heart:
                counters = await run_sweep(
                    heart,
                    settings,
                    max_pairs=max_pairs,
                    batch_size=batch_size,
                    dry_run=True,
                )
                _print_report(counters, heart, agent_id, watermark, dry_run=True)

        return 0

    except Exception:
        logger.exception("Backfill failed")
        return 2
    finally:
        await db.disconnect()


def _print_report(counters: dict, heart, agent_id: str, watermark: str, *, dry_run: bool) -> None:
    """Print the final report block."""
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"\n=== Supersession Backfill Report ({mode}) ===")
    print(f"  agent_id          : {agent_id}")
    print(f"  watermark         : {watermark}")
    print(f"  pairs_examined    : {counters['pairs_examined']}")
    print(f"  resolutions_written: {counters['resolutions_written']}")
    print(f"  keep_both         : {counters['keep_both']}")

    if dry_run:
        print(
            f"\nDRY RUN: {counters['pairs_examined']} candidate pairs found -- "
            "no writes performed."
        )
        return

    samples = counters.get("sample_resolutions", [])
    if samples:
        print(f"\n=== R2.6 Sampled Resolutions (first {len(samples)}) ===")
        for i, (c1, c2) in enumerate(samples, 1):
            print(f"\n  [{i}] older: {c1[:150]!r}")
            print(f"       newer: {c2[:150]!r}")


def _print_chain_histogram(hist: Counter) -> None:
    """Print the chain-depth histogram."""
    if not hist:
        return
    print("\n=== Chain-Depth Histogram (winners from this run) ===")
    print(f"  {'depth':>5}  {'chains':>6}")
    for depth in sorted(hist):
        print(f"  {depth:>5}  {hist[depth]:>6}")
    total_chains = sum(hist.values())
    print(f"  Total winners with chains: {total_chains}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="064 R2.5/R2.6: resolve same-key fact conflicts via F027 classifier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ROLLBACK: see script header for UPDATE/DELETE SQL.\n"
            "BUDGET: --classifier-budget 0 (default) is unlimited — recommended\n"
            "  for offline runs; at the live 500/hr cap a large run stalls for hours."
        ),
    )
    parser.add_argument(
        "--agent-id", required=True,
        help="Agent identifier (e.g. nous-default).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count candidate pairs only; no classifier calls, no writes.",
    )
    parser.add_argument(
        "--max-pairs", type=int, default=0,
        help="Max pairs to examine (0 = unlimited).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=25,
        help="Pairs fetched per DB round-trip (default: 25).",
    )
    parser.add_argument(
        "--classifier-budget", type=int, default=0,
        help=(
            "Hourly Haiku call cap for this process (0 = unlimited). "
            "At the live default (500/hr) a hundreds-of-chains backfill stalls for hours. "
            "Maps to supersession_classifier_max_per_hour on a process-local Settings copy."
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

    sys.exit(asyncio.run(_run_backfill(
        agent_id=args.agent_id,
        max_pairs=args.max_pairs,
        batch_size=args.batch_size,
        classifier_budget=args.classifier_budget,
        dry_run=args.dry_run,
    )))


if __name__ == "__main__":
    main()
