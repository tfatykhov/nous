"""One-time backfill: retrofit auto_link for decisions that pre-date the
constraint-name fix (see fix/auto-link-constraint-name PR).

Context: ``Brain._auto_link`` referenced a Postgres constraint name that
never existed (``uq_edges_src_tgt_rel`` vs the real auto-named
``graph_edges_source_id_target_id_relation_key``). Every ``record_decision``
call's auto-link attempt raised ``UndefinedObject`` silently, so no
similarity-based decision<->decision edges have ever been written.

Sleep cycle's ``backfill_orphan_decisions`` only recovers TOTAL orphans
(decisions with zero edges). Most prod decisions acquired F022 cross-type
edges (``evidence_for`` from facts, ``discussed_in`` from episodes) right
after creation, making them non-orphans -> sleep cycle skips them -> the
missing similarity edges stay permanently missing.

This script iterates every decision for an agent and calls
``brain.auto_link(decision_id)`` — same code path as ``record_decision``,
just deferred. Idempotent: the now-fixed ``ON CONFLICT DO NOTHING`` skips
edges that already exist (including any that sleep cycle recovered).

Usage::

    # dry run — count decisions only
    uv run python scripts/backfill_auto_link_decisions.py \
        --agent-id nous-default --dry-run

    # full backfill (prod)
    uv run python scripts/backfill_auto_link_decisions.py \
        --agent-id nous-default

    # capped first run
    uv run python scripts/backfill_auto_link_decisions.py \
        --agent-id nous-default --max-decisions 50

Honesty note: this is a *historical* repair. New ``record_decision``
calls auto-link correctly post-fix. This script is only needed once
after the fix deploys; subsequent decisions don't require it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from uuid import UUID

from sqlalchemy import text

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.storage.database import Database

logger = logging.getLogger("auto-link-backfill")


async def _all_decision_ids(
    db: Database, agent_id: str, limit: int | None,
) -> list[UUID]:
    """All decision IDs for an agent that have an embedding. Decisions
    without embeddings can't be cosine-linked anyway; auto_link short-
    circuits on them at brain.py:1516."""
    sql = (
        "SELECT id FROM brain.decisions "
        "WHERE agent_id = :a AND embedding IS NOT NULL "
        "ORDER BY created_at ASC"
    )
    if limit is not None:
        sql += " LIMIT :lim"
    async with db.engine.begin() as conn:
        result = await conn.execute(
            text(sql),
            {"a": agent_id, "lim": limit} if limit is not None else {"a": agent_id},
        )
        return [row.id for row in result.all()]


async def _count_existing_auto_link_edges(db: Database, agent_id: str) -> int:
    async with db.engine.begin() as conn:
        r = await conn.execute(
            text(
                "SELECT COUNT(*) FROM brain.graph_edges "
                "WHERE agent_id = :a AND auto_linked = true "
                "  AND source_type = 'decision' AND target_type = 'decision' "
                "  AND relation = 'related_to'"
            ),
            {"a": agent_id},
        )
        return int(r.scalar() or 0)


async def run_backfill(
    *, agent_id: str, max_decisions: int | None, dry_run: bool,
    threshold: float | None,
) -> int:
    settings = Settings()
    db = Database(settings)
    await db.connect()
    try:
        ids = await _all_decision_ids(db, agent_id, max_decisions)
        before = await _count_existing_auto_link_edges(db, agent_id)
        print(
            f"Agent {agent_id}: {len(ids)} decisions to process "
            f"(currently {before} auto_linked decision<->decision edges; "
            f"threshold={'default' if threshold is None else threshold})"
        )
        if dry_run:
            print("--dry-run set; not writing edges. Exiting.")
            return 0
        if not ids:
            print("Nothing to backfill.")
            return 0

        embedder = EmbeddingProvider(settings)
        # Codex P1: Brain reads agent_id from settings (Brain.__init__),
        # and _auto_link filters/inserts under self.agent_id. If we pass
        # the env-default Settings here, the script silently writes
        # graph_edges rows under the WRONG agent — and reads similar
        # decisions from a different tenant — even though we loaded
        # decision IDs from --agent-id. Construct a Settings copy with
        # the requested agent_id so Brain operates in the right tenant.
        scoped_settings = settings.model_copy(update={"agent_id": agent_id})
        brain = Brain(db, scoped_settings, embedder)
        assert brain.agent_id == agent_id, (
            f"Brain.agent_id ({brain.agent_id!r}) must match --agent-id "
            f"({agent_id!r}) — refusing to write cross-tenant edges."
        )

        created = 0
        failed = 0
        start = time.time()
        for i, did in enumerate(ids, 1):
            try:
                edges = await brain.auto_link(did, threshold=threshold)
                created += len(edges)
            except Exception:
                # The pre-fix bug would land here; post-fix this should
                # only fire on genuinely unexpected errors. exc_info so
                # we can diagnose if it happens.
                logger.warning(
                    "auto_link failed for decision %s during retrofit",
                    did, exc_info=True,
                )
                failed += 1
            if i % 50 == 0 or i == len(ids):
                elapsed = (time.time() - start) / 60.0
                print(
                    f"  processed {i:>5d} / {len(ids):>5d}  "
                    f"created={created:>5d}  failed={failed}  "
                    f"[{elapsed:.1f} min]",
                    flush=True,
                )

        after = await _count_existing_auto_link_edges(db, agent_id)
        print()
        print(
            f"Done. auto_linked dec<->dec edges: {before} -> {after}  "
            f"(+{after - before} new). Per-call edges reported by auto_link "
            f"totaled {created} (some may collide via ON CONFLICT)."
        )
        if failed:
            print(f"WARNING: {failed} decisions failed; see log.", file=sys.stderr)
        return 0 if failed == 0 else 3
    finally:
        await db.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time auto_link retrofit for historical decisions.",
    )
    parser.add_argument(
        "--agent-id", required=True,
        help="Agent identifier whose decisions to retrofit.",
    )
    parser.add_argument(
        "--max-decisions", type=int, default=None,
        help="Cap on decisions processed. Omit for unbounded.",
    )
    parser.add_argument(
        "--threshold", type=float, default=None,
        help="Override cosine threshold. Default uses Settings.auto_link_threshold (0.85).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print decision count and exit without writing.",
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
        max_decisions=args.max_decisions,
        dry_run=args.dry_run,
        threshold=args.threshold,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
