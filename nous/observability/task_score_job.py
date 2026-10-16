"""Task scoring v1: the one-shot command (autonomy plan v2, A1).

    python -m nous.observability.task_score_job --agent-id nous-default

scores every subtask, DAG run and intention root that finished in the last ``SCORING_WINDOW_DAYS`` into
``scoring.task_scores`` and exits: 0 when the pass wrote, 1 when it failed (so the service wrapper can tell). It is
not a loop inside the app and has no setting: the ``nous-scorer`` compose service (defined by the integrity plan)
runs it every 900 s with its own database role, which keeps the writer of the scores outside the agent's process.
The job needs only SELECT on the source tables and INSERT and UPDATE on ``scoring.task_scores``: no DDL, no temp
table, no advisory lock, no delete. ``GET /dashboard/autonomy`` shows ``last_scored_at``, so a dead scorer is visible.
Owner grades and undos arrive days after a task ends, so every pass re-scores the whole
window; a row is ``settled`` once its task is older than ``SETTLE_DAYS``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.observability.task_score_gatherer import gather_signals
from nous.observability.task_scoring import (
    SCORING_WINDOW_DAYS,
    TaskScore,
    score_leaf,
    score_rollup,
)
from nous.storage.models import TaskScore as TaskScoreRow

if TYPE_CHECKING:
    from nous.storage.database import Database

logger = logging.getLogger(__name__)

UPSERT_CHUNK = 200  # rows per INSERT: 21 columns each, far under asyncpg's 32767 bind parameters
_KEY = ("agent_id", "task_kind", "task_id", "scorer_version")
# The privilege contract of the scoring role: SELECT on these tables, SELECT + INSERT + UPDATE on SCORE_TABLE
# (ON CONFLICT DO UPDATE reads the row), USAGE on their schemas, and nothing else. tests/test_task_score_job.py
# runs a pass as a role holding exactly this, so a new table in the gatherer fails there before it fails in prod.
SCORE_TABLE = "scoring.task_scores"
SOURCE_TABLES = (
    "brain.decisions",
    "brain.intention_arrivals",
    "brain.intention_proposals",
    "brain.intentions",
    "heart.subtasks",
    "nous_system.compensation_snapshots",
    "nous_system.dag_nodes",
    "nous_system.events",
    "nous_system.execution_dags",
    "nous_system.execution_ledger",
)
# first_scored_at is not here: it is set on insert and never changed.
_REFRESHED = (
    "finished_at",
    "outcome",
    "not_scored_reason",
    "owner_grade",
    "owner_overrides",
    "unverified_claims",
    "harness_errors",
    "refusals",
    "tokens",
    "score",
    "children_scored",
    "children_mean_score",
    "settled",
    "evidence",
    "scored_at",
)


async def upsert_scores(
    session: AsyncSession, agent_id: str, scores: Sequence[TaskScore], *, scored_at: datetime
) -> int:
    """Insert each score, or refresh the row with the same (agent, kind, task, scorer_version). Does not commit.

    A row of another ``scorer_version`` is never touched (re-scoring under a new version adds rows), and a row that is
    already ``settled`` is left as it is.
    """
    for start in range(0, len(scores), UPSERT_CHUNK):
        rows = [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "task_kind": s.task_kind,
                "task_id": s.task_id,
                "finished_at": s.finished_at,
                "scorer_version": s.scorer_version,
                "outcome": s.outcome,
                "not_scored_reason": s.not_scored_reason,
                "owner_grade": s.owner_grade,
                "owner_overrides": s.owner_overrides,
                "unverified_claims": s.unverified_claims,
                "harness_errors": s.harness_errors,
                "refusals": s.refusals,
                "tokens": s.tokens,
                "score": s.score,
                "children_scored": s.children_scored,
                "children_mean_score": s.children_mean_score,
                "settled": s.settled,
                "evidence": s.evidence,
                "first_scored_at": scored_at,
                "scored_at": scored_at,
            }
            for s in scores[start : start + UPSERT_CHUNK]
        ]
        stmt = pg_insert(TaskScoreRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(_KEY),
            set_={column: stmt.excluded[column] for column in _REFRESHED},
            # A settled row is final: it is not rewritten, so a later pass can never move a settled score.
            where=TaskScoreRow.settled.is_(False),
        )
        await session.execute(stmt)
    return len(scores)


async def run_scoring_pass(
    database: Database, agent_id: str, *, window_days: int = SCORING_WINDOW_DAYS, now: datetime | None = None
) -> int:
    """Score every task of ``agent_id`` finished in the window and upsert the rows in one transaction.

    The leaves are scored first: the roll-ups read their scores. Returns the number of rows written.
    """
    now = now or datetime.now(UTC)
    async with database.session() as session:
        gathered = await gather_signals(session, agent_id, since=now - timedelta(days=window_days))
        leaves = [row for sig in gathered.leaves if (row := score_leaf(sig, now)) is not None]
        leaf_scores = {row.task_id: row.score for row in leaves}
        rollups = [
            row
            for sig in (*gathered.dags, *gathered.chains)
            if (row := score_rollup(sig, leaf_scores, now)) is not None
        ]
        written = await upsert_scores(session, agent_id, [*leaves, *rollups], scored_at=now)
        await session.commit()
    logger.info(
        "Autonomy A1: wrote %d row(s): %d subtask(s), %d DAG run(s), %d intention root(s) (agent=%s, window=%dd)",
        written,
        len(leaves),
        len(gathered.dags),
        len(gathered.chains),
        agent_id,
        window_days,
    )
    return written


def _open_database() -> Database:
    from nous.config import Settings
    from nous.storage.database import Database

    return Database(Settings())


async def _run(agent_id: str | None, window_days: int) -> int:
    # No database.connect(): its schema probe reads information_schema for the three app schemas, which the
    # scoring role does not need to see. The engine connects on the first query.
    database = _open_database()
    try:
        if agent_id is None:
            from nous.config import Settings

            agent_id = Settings().agent_id
        await run_scoring_pass(database, agent_id, window_days=window_days)
    finally:
        await database.disconnect()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The command. 0 when the pass wrote its rows, 1 when anything failed."""
    parser = argparse.ArgumentParser(
        prog="python -m nous.observability.task_score_job", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--agent-id", default=None, help="the agent to score (default: NOUS_AGENT_ID)")
    parser.add_argument(
        "--window-days", type=int, default=SCORING_WINDOW_DAYS, help="days of finished tasks to re-score"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return asyncio.run(_run(args.agent_id, args.window_days))
    except Exception:
        logger.exception("Autonomy A1: the scoring pass failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
