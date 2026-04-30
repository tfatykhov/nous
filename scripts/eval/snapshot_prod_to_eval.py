"""F056 follow-up: snapshot Tim's production Nous data to eval DB.

Read-only against prod (192.168.1.141), write to eval DB (nous_eval_scratch)
under agent_id=`nous-prod-snapshot` so the snapshot doesn't collide with
existing eval agent_ids (LongMemEval, handler-eval scopes).

Tables snapshotted:
- heart.facts (with embeddings)
- heart.episodes (with embeddings + structured_summary)
- heart.procedures (with embeddings)
- brain.decisions (with embeddings)
- brain.graph_edges

Source agent_id: read from `--source-agent-id` (default `nous-default`).
Target agent_id: `nous-prod-snapshot` (hardcoded — single namespace).

Usage:
    set -a; . .env; set +a
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/snapshot_prod_to_eval.py \
        --prod-host 192.168.1.141 \
        [--source-agent-id nous-default]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import OrderedDict

import asyncpg


_TARGET_AGENT_ID = "nous-prod-snapshot"

# Tables to copy. Order matters — entities BEFORE edges so foreign-keyed
# refs land in eval DB first.
_TABLE_SCHEMAS = OrderedDict([
    ("heart.facts", {"id_col": "id"}),
    ("heart.episodes", {"id_col": "id"}),
    ("heart.procedures", {"id_col": "id"}),
    ("brain.decisions", {"id_col": "id"}),
    ("brain.graph_edges", {"id_col": "id"}),
])


async def _list_columns(conn: asyncpg.Connection, schema: str, table: str) -> list[str]:
    """Return column names for `schema.table` in their native order."""
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """,
        schema, table,
    )
    return [r["column_name"] for r in rows]


async def _copy_table(
    prod: asyncpg.Connection,
    eval_: asyncpg.Connection,
    full_table: str,
    source_agent_id: str,
    target_agent_id: str,
    logger: logging.Logger,
) -> int:
    """Copy WHERE agent_id matches; rewrite agent_id on insert. Returns row count."""
    schema, table = full_table.split(".")
    cols = await _list_columns(prod, schema, table)
    # Filter out search_tsv (DB-generated, can't be inserted)
    cols = [c for c in cols if c != "search_tsv"]
    if "agent_id" not in cols:
        logger.warning("%s has no agent_id column; skipping", full_table)
        return 0

    col_csv = ", ".join(cols)
    placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
    agent_id_idx = cols.index("agent_id")

    # Read from prod
    rows = await prod.fetch(
        f"SELECT {col_csv} FROM {full_table} WHERE agent_id = $1",
        source_agent_id,
    )
    logger.info("%s: %d rows from prod (agent_id=%s)", full_table, len(rows), source_agent_id)
    if not rows:
        return 0

    # Truncate any existing snapshot rows for this agent_id (idempotent re-runs).
    await eval_.execute(
        f"DELETE FROM {full_table} WHERE agent_id = $1",
        target_agent_id,
    )

    # Bulk insert with agent_id rewritten
    insert_sql = f"INSERT INTO {full_table} ({col_csv}) VALUES ({placeholders})"
    inserted = 0
    for row in rows:
        values = list(row.values())
        values[agent_id_idx] = target_agent_id
        try:
            await eval_.execute(insert_sql, *values)
            inserted += 1
        except Exception as exc:
            logger.warning("%s row insert failed: %s", full_table, exc)

    logger.info("%s: %d rows inserted to eval DB (agent_id=%s)", full_table, inserted, target_agent_id)
    return inserted


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prod-host", default=os.environ.get("PROD_DB_HOST", "192.168.1.141"))
    p.add_argument("--prod-port", type=int, default=int(os.environ.get("DB_PORT", "5432")))
    p.add_argument("--prod-user", default=os.environ.get("DB_USER", "nous"))
    p.add_argument("--prod-password", default=os.environ.get("DB_PASSWORD"))
    p.add_argument("--prod-db", default=os.environ.get("DB_NAME", "nous"))
    p.add_argument("--source-agent-id", default="nous-default")
    p.add_argument("--eval-host", default="127.0.0.1")
    p.add_argument("--eval-port", type=int, default=5433)
    p.add_argument("--eval-db", default=os.environ.get("NOUS_EVAL_DB_NAME", "nous_eval_scratch"))
    p.add_argument("--eval-user", default="nous")
    p.add_argument("--eval-password", default=os.environ.get("NOUS_EVAL_DB_PASSWORD", "nous_eval"))
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    if not args.prod_password:
        print("ERROR: prod DB_PASSWORD not set in env or .env", file=sys.stderr)
        return 2

    logger.info(
        "Connecting prod=%s:%d/%s eval=%s:%d/%s",
        args.prod_host, args.prod_port, args.prod_db,
        args.eval_host, args.eval_port, args.eval_db,
    )

    prod = await asyncpg.connect(
        host=args.prod_host, port=args.prod_port,
        user=args.prod_user, password=args.prod_password,
        database=args.prod_db,
    )
    eval_ = await asyncpg.connect(
        host=args.eval_host, port=args.eval_port,
        user=args.eval_user, password=args.eval_password,
        database=args.eval_db,
    )

    try:
        # Register pgvector type so embedding columns round-trip cleanly
        try:
            from pgvector.asyncpg import register_vector
            await register_vector(prod)
            await register_vector(eval_)
        except ImportError:
            logger.warning("pgvector.asyncpg not available; vector cols may fail")

        total_rows = 0
        for full_table in _TABLE_SCHEMAS:
            n = await _copy_table(
                prod, eval_, full_table,
                source_agent_id=args.source_agent_id,
                target_agent_id=_TARGET_AGENT_ID,
                logger=logger,
            )
            total_rows += n

        # Verify
        print()
        print("=" * 70)
        print(f"SNAPSHOT COMPLETE: {args.source_agent_id} -> {_TARGET_AGENT_ID}")
        print("=" * 70)
        for full_table in _TABLE_SCHEMAS:
            count = await eval_.fetchval(
                f"SELECT COUNT(*) FROM {full_table} WHERE agent_id = $1",
                _TARGET_AGENT_ID,
            )
            print(f"  {full_table:25s}: {count}")
        print(f"  {'TOTAL':25s}: {total_rows}")
        print("=" * 70)

    finally:
        await prod.close()
        await eval_.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
