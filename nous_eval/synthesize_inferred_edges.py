"""F051 eval-only synthesis: relabel auto-linker edges as `inferred`.

Production today writes almost no `contradicts`-relation edges (the F027
classifier is deliberately biased toward UPDATE/supersedes — see
`nous/heart/facts.py:35-70`), so migration 047's backfill rule leaves
`extraction_method='inferred'` empty. That makes the F065 penalty
multiplier a no-op in every eval run: zero inferred-tier edges to weigh.

This script mutates the **eval DB only** (never prod) to seed inferred
labels onto a subset of heuristic edges that resemble the population the
F065 penalty was conceptually designed to target: cosine-auto-linked
`related_to` rows from the event-bus auto-linker. The goal is to
validate the penalty hypothesis empirically before committing to a
production migration that would do the same thing for real (see
task #29 / Step 3).

Usage:
    python -m nous_eval.synthesize_inferred_edges \\
        --agent-id nous-prod-snapshot \\
        --relation related_to

The eval DB host/port/db_name come from NOUS_EVAL_DB_* env vars (same
as the retrieval harness).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db

logger = logging.getLogger(__name__)


async def _relabel(
    db: Database, agent_id: str, relation: str, dry_run: bool
) -> tuple[int, int]:
    """Returns (eligible_count, updated_count)."""
    async with db.session() as session:
        count_row = (await session.execute(
            text("""
                SELECT COUNT(*)
                FROM brain.graph_edges
                WHERE agent_id = :agent_id
                  AND relation = :relation
                  AND extraction_method = 'heuristic'
                  AND auto_linked = true
            """),
            {"agent_id": agent_id, "relation": relation},
        )).scalar_one()
        eligible = int(count_row or 0)

        if dry_run or eligible == 0:
            return eligible, 0

        result = await session.execute(
            text("""
                UPDATE brain.graph_edges
                SET extraction_method = 'inferred'
                WHERE agent_id = :agent_id
                  AND relation = :relation
                  AND extraction_method = 'heuristic'
                  AND auto_linked = true
            """),
            {"agent_id": agent_id, "relation": relation},
        )
        await session.commit()
        return eligible, int(result.rowcount or 0)


async def _summary(db: Database, agent_id: str) -> dict[str, int]:
    async with db.session() as session:
        rows = (await session.execute(
            text("""
                SELECT extraction_method, COUNT(*) AS c
                FROM brain.graph_edges
                WHERE agent_id = :agent_id
                GROUP BY extraction_method
            """),
            {"agent_id": agent_id},
        )).all()
    return {r.extraction_method: int(r.c) for r in rows}


async def _run_async(args: argparse.Namespace) -> int:
    eval_settings = EvalSettings()
    eval_settings.warn_if_default_password()
    base_settings = Settings()
    main_settings = _settings_for_eval_db(eval_settings, base_settings)

    db = Database(settings=main_settings)
    await db.connect()
    try:
        before = await _summary(db, args.agent_id)
        logger.info("BEFORE: %s", before)
        eligible, updated = await _relabel(
            db, args.agent_id, args.relation, args.dry_run
        )
        after = await _summary(db, args.agent_id)
        logger.info("ELIGIBLE matching filter: %d", eligible)
        if args.dry_run:
            logger.info("DRY RUN — no rows changed")
        else:
            logger.info("UPDATED %d rows", updated)
        logger.info("AFTER: %s", after)
    finally:
        await db.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nous_eval.synthesize_inferred_edges",
        description="Relabel auto-linker edges as 'inferred' on the eval DB only.",
    )
    parser.add_argument("--agent-id", default="nous-prod-snapshot",
                        help="Eval DB agent_id whose edges to relabel.")
    parser.add_argument("--relation", default="related_to",
                        help="Relation to relabel. Default `related_to`.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show counts without mutating.")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
