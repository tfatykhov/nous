"""One-off: close episodes that F060 recovery summarized but left open.

Before the fix, F060 Loop A wrote ``structured_summary`` on an abandoned episode
and left it ``active=true, ended_at NULL, outcome NULL``, so it ranked as a live
episode forever. An open row with a structured summary is exactly such a row:
the summarizer's own path runs after ``_end``. This closes them with the same
SET clause and the same live-session guard as the fixed sleep phase (no
working-memory write for the episode's session inside the min-age window).
Each row ends at episode_close_at_sql's close_at: where its decision window
already ended when a later episode of its session bounds it, otherwise at its
last real activity, otherwise at its start with duration_seconds left NULL. It
needs an image that contains that helper (#825).

For the agent container:
    docker exec -i nous-agent python - < scripts/diag/close_recovered_episodes.py
    docker exec -i nous-agent python - --apply < scripts/diag/close_recovered_episodes.py

--dry-run (default) prints the count, then one ``id,started_at,session_id`` CSV
line per row. --apply closes them all in one transaction (no per-run cap, unlike
the sleep sweep) and prints the rowcount.
Prints ids, timestamps and counts only, never episode content.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from nous.brain.graph_constants import episode_close_at_sql
from nous.config import Settings
from nous.storage.database import Database

_SELECT = """
    SELECT e.id FROM heart.episodes e
    WHERE e.agent_id = :agent_id
      AND e.active = true
      AND e.ended_at IS NULL
      AND e.structured_summary IS NOT NULL
      AND e.started_at < now() - make_interval(hours => :hours)
      AND NOT EXISTS (
        SELECT 1 FROM heart.working_memory wm
        WHERE wm.agent_id = e.agent_id
          AND wm.session_id = e.session_id
          AND wm.updated_at >= now() - make_interval(hours => :hours)
      )
"""

_LIST = f"""
    SELECT id, started_at, session_id FROM heart.episodes
    WHERE id IN ({_SELECT})
    ORDER BY started_at ASC
"""

_CLOSE = f"""
    UPDATE heart.episodes h
    SET ended_at = c.close_at,
        duration_seconds = COALESCE(
            h.duration_seconds,
            CASE WHEN c.end_known THEN EXTRACT(EPOCH FROM c.close_at - h.started_at)::int END
        ),
        active = false,
        outcome = COALESCE(h.outcome, 'partial')
    FROM ({episode_close_at_sql(_SELECT)}) c
    WHERE c.id = h.id
      AND h.agent_id = :agent_id
      AND h.ended_at IS NULL
"""


async def main() -> int:
    settings = Settings()
    ap = argparse.ArgumentParser(description="Close episodes F060 recovered but left open")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list the rows (default)")
    mode.add_argument("--apply", action="store_true", help="close them in one transaction")
    ap.add_argument(
        "--min-age-hours",
        type=int,
        default=settings.abandoned_recovery_min_age_hours,
        help="episode age and session idle window (default: abandoned_recovery_min_age_hours)",
    )
    args = ap.parse_args()
    if args.min_age_hours < 1:
        ap.error("--min-age-hours must be >= 1")
    params = {"agent_id": settings.agent_id, "hours": args.min_age_hours}

    db = Database(settings)
    await db.connect()
    try:
        async with db.engine.connect() as conn:
            if not args.apply:
                rows = (await conn.execute(text(_LIST), params)).all()
                print(f"DRY-RUN agent={settings.agent_id} min_age_hours={args.min_age_hours} count={len(rows)}")
                print("id,started_at,session_id")
                for row in rows:
                    print(f"{row.id},{row.started_at.isoformat()},{row.session_id or ''}")
                return 0
            async with conn.begin():
                result = await conn.execute(text(_CLOSE), params)
            print(f"APPLY agent={settings.agent_id} min_age_hours={args.min_age_hours} closed={result.rowcount}")
            return 0
    finally:
        await db.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
