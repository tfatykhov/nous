"""Task scoring v1: the one-shot command.

    python -m nous.observability.task_score_job --agent-id nous-default

scores every subtask, DAG run and intention root that finished in the last ``SCORING_WINDOW_DAYS`` into
``scoring.task_scores`` and exits: 0 when the pass committed, 1 when it failed (so the service wrapper can tell). It is
not a loop inside the app and has no setting: it is meant to run on a timer as a separate service under its own
database role, which keeps the writer of the scores outside the agent's process.
The job needs only SELECT on the source tables and SELECT, INSERT and UPDATE on ``scoring.task_scores`` (it reads
the oldest provisional row and the stored scores of roll-up children there too) and on ``scoring.scorer_heartbeat``:
no DDL, no temp table, no advisory lock, no delete. A pass row-locks the agent's heartbeat row first
(``SELECT ... FOR UPDATE``, which UPDATE on that table allows), so two passes for one agent (the timer and a manual
backfill) take turns: the second reads the PR credits the first committed (one PR credits one leaf).
Every pass upserts the agent's row in ``scoring.scorer_heartbeat``: ``ok`` in the same transaction as the scores, or
``error`` with the exception's class name in a transaction of its own (best effort: it never hides the pass's error).
That row is the liveness signal, shown as ``scorer`` by ``GET /dashboard/autonomy``: a ``last_run_at`` much older than
the scorer's interval means the scorer stopped or cannot write to ``scoring``. ``last_scored_at`` stays the time of
the latest score write, which does not move while no task finished in the last 14 days.
Owner grades and undos arrive days after a task ends, so every pass re-scores the whole
window; a row is ``settled`` once its task is older than ``SETTLE_DAYS``. A row still provisional although its task
finished before the window start (no pass ran in the day it had to settle) pulls the window back to that task, so
it is re-scored and settled: ``--window-days`` is the least a pass covers, not the most. The reach-back stops at
``MAX_REACHBACK_DAYS``: an older provisional row stays provisional and each pass logs a WARNING with how many there
are and when the oldest finished.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Text, cast, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.audits import AUDIT_VERDICT_ACTION
from nous.dag.approval import as_utc
from nous.log_redaction import configure_logging
from nous.observability.github_verify import GitHubReader, PrVerifier, StoredPrEvidence, stored_pr_evidence
from nous.observability.task_score_gatherer import audits_without_action_row, chunked, gather_signals
from nous.observability.task_scoring import (
    LEAF_KIND,
    OUTCOMES,
    SCORER_VERSION,
    SCORING_WINDOW_DAYS,
    TaskScore,
    score_leaf,
    score_rollup,
)
from nous.storage.models import ScorerHeartbeat as ScorerHeartbeatRow
from nous.storage.models import TaskScore as TaskScoreRow

if TYPE_CHECKING:
    from nous.config import Settings
    from nous.storage.database import Database

logger = logging.getLogger(__name__)

UPSERT_CHUNK = 200  # rows per INSERT: 21 columns each, far under asyncpg's 32767 bind parameters
MAX_REACHBACK_DAYS = 30  # the furthest back a pass reaches to settle a provisional row (older ones are only logged)
ERROR_CHARS = 120  # the width of scorer_heartbeat.last_error
_KEY = ("agent_id", "task_kind", "task_id", "scorer_version")
# The privilege contract of the scoring role: SELECT on these tables, SELECT + INSERT + UPDATE on SCORE_TABLE and on
# HEARTBEAT_TABLE (both are upserts, and ON CONFLICT DO UPDATE reads the row), USAGE on their schemas, and nothing
# else. A pass also reads SCORE_TABLE itself (the oldest provisional row, and the stored scores of roll-up children
# that finished before the window), which that SELECT covers, so SCORE_TABLE is not a source.
# tests/test_task_score_job.py runs a pass as a role holding exactly this, and checks that the tables a pass reads are
# exactly SOURCE_TABLES plus SCORE_TABLE and that it writes only SCORE_TABLE and HEARTBEAT_TABLE, so a new table in
# the gatherer fails there before it fails in prod.
SCORE_TABLE = "scoring.task_scores"
HEARTBEAT_TABLE = "scoring.scorer_heartbeat"
SOURCE_TABLES = (
    "brain.decisions",
    "brain.intention_arrivals",
    "brain.intention_proposals",
    "brain.intentions",
    "heart.subtasks",
    "nous_system.a2ui_actions",  # v5: the tripwire, audits_without_action_row
    "nous_system.check_audits",  # v5: the owner's audits (migration 090)
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
    already ``settled`` is left as it is. Returns the number of rows submitted, a settled row the database skips
    included: the drivers do not agree on what a multi-row upsert reports as its row count.
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


async def upsert_heartbeat(
    session: AsyncSession,
    agent_id: str,
    *,
    written_at: datetime,
    submitted: int | None = None,
    error: Exception | None = None,
) -> None:
    """Insert or overwrite the agent's heartbeat row, stamped ``written_at``: the time of this write, not the pass's
    own ``now``, so a slow pass does not record a run that already looks stale. Does not commit.

    Without ``error`` the pass is committing ``submitted`` rows: ``ok``, and ``last_ok_at`` too. With one the pass
    failed: ``error`` and the exception's class name, never its message (it can carry data, the log has it), and the
    ``last_ok_at`` and ``rows_submitted`` of the last committed pass are left as they were.
    """
    row: dict[str, object] = {
        "agent_id": agent_id,
        "scorer_version": SCORER_VERSION,
        "last_run_at": written_at,
        "last_status": "ok" if error is None else "error",
        "last_error": None if error is None else type(error).__name__[:ERROR_CHARS],
    }
    if error is None:
        row |= {"last_ok_at": written_at, "rows_submitted": submitted}
    stmt = pg_insert(ScorerHeartbeatRow).values(row)
    # Only the columns this pass sets are overwritten, so a failed pass keeps the time and count of the last ok one.
    stmt = stmt.on_conflict_do_update(
        index_elements=["agent_id"], set_={column: stmt.excluded[column] for column in row if column != "agent_id"}
    )
    await session.execute(stmt)


async def _stored_leaf_scores(session: AsyncSession, agent_id: str, task_ids: Sequence[str]) -> dict[str, float | None]:
    """task id -> the stored score of each of these subtasks: its row under this ``SCORER_VERSION``, or else its row
    under the newest earlier version.

    A roll-up settles at ``SETTLE_DAYS`` but the window is one day longer, so a child that finished more than a day
    before its DAG or chain closed is out of the window when the parent settles. Its stored row stands in for it. Right
    after a version bump such a child has rows of earlier versions only (a pass gathers the window, not the child), so
    the newest of those stands in rather than the child going uncounted for good once the parent settles. A child never
    scored has no row and stays uncounted; a stored ``not_scored`` row has score NULL and is not counted.
    """
    found: dict[str, float | None] = {}
    for chunk in chunked(list(task_ids)):
        result = await session.execute(
            select(TaskScoreRow.task_id, TaskScoreRow.score)
            .where(
                TaskScoreRow.agent_id == agent_id,
                TaskScoreRow.task_kind == LEAF_KIND,
                TaskScoreRow.scorer_version <= SCORER_VERSION,
                TaskScoreRow.task_id.in_(chunk),
            )
            .order_by(TaskScoreRow.scorer_version)  # ascending, so the newest version's row is the one kept
        )
        found.update({r.task_id: r.score for r in result})
    return found


async def _provisional_rows(
    session: AsyncSession, agent_id: str, floor: datetime
) -> tuple[datetime | None, int, datetime | None]:
    """This agent's rows under this ``SCORER_VERSION`` that are not settled yet, split at ``floor``: when the oldest
    task at or after it finished, and how many finished before it and when the oldest of those did."""
    T = TaskScoreRow
    provisional = (T.agent_id == agent_id, T.scorer_version == SCORER_VERSION, T.settled.is_(False))
    oldest = (await session.execute(select(func.min(T.finished_at)).where(*provisional, T.finished_at >= floor))).one()
    beyond = (
        await session.execute(select(func.count(), func.min(T.finished_at)).where(*provisional, T.finished_at < floor))
    ).one()
    return as_utc(oldest[0]), beyond[0], as_utc(beyond[1])


def pass_lock(agent_id: str):
    """The statement that serialises the passes of one agent: a row lock on its heartbeat row, held until the pass's
    transaction ends. No row yet (the agent's very first pass) locks nothing, which only a first pass can race."""
    return select(ScorerHeartbeatRow.agent_id).where(ScorerHeartbeatRow.agent_id == agent_id).with_for_update()


async def _stored_pr_evidence(session: AsyncSession, agent_id: str) -> StoredPrEvidence:
    """The PR pins and credits earlier passes recorded, any scorer version from 4 on and settled or not: one PR
    credits one leaf for good, even after that leaf's row has left the window."""
    result = await session.execute(
        select(TaskScoreRow.task_id, TaskScoreRow.evidence)
        .where(
            TaskScoreRow.agent_id == agent_id,
            TaskScoreRow.task_kind == LEAF_KIND,
            TaskScoreRow.scorer_version >= 4,
            cast(TaskScoreRow.evidence, Text).like("%v2_pr%"),
        )
        .order_by(TaskScoreRow.first_scored_at, TaskScoreRow.task_id)
    )
    return stored_pr_evidence((r.task_id, r.evidence) for r in result)


OUTCOME_CONSTRAINT = "ck_task_scores_outcome"


def outcomes_missing_from(constraint_def: str | None) -> list[str]:
    """The scorer's outcomes the stored CHECK definition does not admit (empty for a definition it cannot read)."""
    if not constraint_def:
        return []
    return [outcome for outcome in OUTCOMES if f"'{outcome}'" not in constraint_def]


async def warn_if_outcome_constraint_is_stale(session: AsyncSession, agent_id: str) -> list[str]:
    """Postgres only: read the outcome CHECK of ``scoring.task_scores`` once and log an ERROR naming what it lacks. A
    scorer newer than the database's constraint (a deploy that skipped ``scripts/ops/db_roles.sql``, which widens it:
    the app role cannot alter the scoring tables) fails its whole pass on the first row of a new outcome. This only
    says so early; it changes no score and never fails the pass. SQLite has no such constraint to read."""
    if session.sync_session.get_bind().dialect.name != "postgresql":
        return []
    try:
        async with session.begin_nested():
            found = await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'scoring.task_scores'::regclass AND conname = :name"
                ),
                {"name": OUTCOME_CONSTRAINT},
            )
            definition = found.scalar_one_or_none()
    except Exception:
        logger.warning("Task scores: could not read %s (agent=%s)", OUTCOME_CONSTRAINT, agent_id, exc_info=True)
        return []
    missing = outcomes_missing_from(definition)
    if missing:
        logger.error(
            "Task scores: %s does not admit outcome(s) %s, so rows of those outcomes will be refused and the pass "
            "will fail: re-run scripts/ops/db_roles.sql (agent=%s)",
            OUTCOME_CONSTRAINT,
            ", ".join(missing),
            agent_id,
        )
    return missing


async def run_scoring_pass(
    database: Database,
    agent_id: str,
    *,
    window_days: int = SCORING_WINDOW_DAYS,
    now: datetime | None = None,
    pr_verifier: PrVerifier | None = None,
) -> int:
    """Score every task of ``agent_id`` finished in the window and upsert the rows in one transaction.

    The window reaches back further when a row is still provisional although its task finished before the window
    start: it is re-scored and settled with fresh signals. It never reaches back more than ``MAX_REACHBACK_DAYS``. The
    leaves are scored first: the roll-ups read their scores. Returns the number of rows submitted to the upsert.
    ``pr_verifier`` decides the leaves whose node passed the PR check (scorer v4); without it they stay unverified.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=window_days)
    floor = min(since, now - timedelta(days=MAX_REACHBACK_DAYS))
    async with database.session() as session:
        await session.execute(pass_lock(agent_id))  # before any read: a concurrent pass for this agent waits here
        # A row settles on the first pass after its task is SETTLE_DAYS old, and the window is only a day longer. With
        # no pass in that day the task leaves the window and its row would stay provisional for good, so the window
        # reaches back to the oldest such row, but not past the floor: a row whose task can no longer be gathered
        # would otherwise hold the window open and widen it every day.
        oldest, beyond, beyond_oldest = await _provisional_rows(session, agent_id, floor)
        if beyond:
            logger.warning(
                "Task scores: %d provisional row(s) finished more than %d days ago and stay provisional, the oldest "
                "at %s (agent=%s)",
                beyond,
                MAX_REACHBACK_DAYS,
                beyond_oldest.isoformat() if beyond_oldest is not None else None,
                agent_id,
            )
        if oldest is not None and oldest < since:
            since = oldest
            logger.info(
                "Task scores: the window reaches back to %s, the oldest provisional row (agent=%s)",
                since.isoformat(),
                agent_id,
            )
        gathered = await gather_signals(session, agent_id, since=since)
        # v5 tripwire: a verdict with no recorded tap was written some other way. Logged, never scored.
        unmatched = await audits_without_action_row(session, agent_id)
        if unmatched:
            logger.warning(
                "Task scores: %d answered check audit(s) have no completed %s action on their card (agent=%s)",
                unmatched,
                AUDIT_VERDICT_ACTION,
                agent_id,
            )
        await warn_if_outcome_constraint_is_stale(session, agent_id)
        leaf_signals = gathered.leaves
        if pr_verifier is not None and any(sig.pr_check is not None for sig in leaf_signals):
            leaf_signals = await pr_verifier.verify(leaf_signals, await _stored_pr_evidence(session, agent_id))
        leaves = [row for sig in leaf_signals if (row := score_leaf(sig, now)) is not None]
        leaf_scores = {row.task_id: row.score for row in leaves}
        provisional = frozenset(row.task_id for row in leaves if not row.settled)  # a held child holds its parent
        missing = sorted({c for sig in (*gathered.dags, *gathered.chains) for c in sig.child_ids} - leaf_scores.keys())
        if missing:  # children that finished before the window: their stored scores, the fresh ones winning
            leaf_scores = {**await _stored_leaf_scores(session, agent_id, missing), **leaf_scores}
        rollups = [
            row
            for sig in (*gathered.dags, *gathered.chains)
            if (row := score_rollup(sig, leaf_scores, now, provisional)) is not None
        ]
        submitted = await upsert_scores(session, agent_id, [*leaves, *rollups], scored_at=now)
        # In the scores' transaction: the heartbeat says ok if and only if the scores committed.
        await upsert_heartbeat(session, agent_id, written_at=datetime.now(UTC), submitted=submitted)
        await session.commit()
    rollup_kinds = Counter(row.task_kind for row in rollups)  # score_rollup can decline one: count what was sent
    logger.info(
        "Task scores: submitted %d row(s): %d subtask(s), %d DAG run(s), %d intention root(s) (agent=%s, window=%dd)",
        submitted,
        len(leaves),
        rollup_kinds["dag"],
        rollup_kinds["intention"],
        agent_id,
        window_days,
    )
    return submitted


def _open_database(settings: Settings) -> Database:
    from nous.storage.database import Database

    return Database(settings)


async def _record_failed_pass(database: Database, agent_id: str, error: Exception) -> None:
    """Write the error heartbeat of a failed pass in a transaction of its own. Best effort: it never raises, so the
    pass's own error is the one ``main`` reports."""
    try:
        async with database.session() as session:
            await upsert_heartbeat(session, agent_id, written_at=datetime.now(UTC), error=error)
            await session.commit()
    except Exception:
        logger.warning("Task scores: the heartbeat could not be written after a failed pass", exc_info=True)


async def _run(agent_id: str | None, window_days: int) -> int:
    from nous.config import Settings

    settings = Settings()
    # No database.connect(): its schema probe reads information_schema for the three app schemas, which the
    # scoring role does not need to see. The engine connects on the first query.
    database = _open_database(settings)
    reader: GitHubReader | None = None
    verifier: PrVerifier | None = None
    try:
        agents = [login for login in settings.scorer_agent_logins.split(",") if login.strip()]
        mergers = [login for login in settings.scorer_merger_logins.split(",") if login.strip()]
        if agents and mergers:
            reader = GitHubReader(settings.scorer_github_token or None, token_name="NOUS_SCORER_GITHUB_TOKEN")
            verifier = PrVerifier(reader, agents, mergers)
        else:
            logger.info(
                "Task scores: NOUS_SCORER_AGENT_LOGINS or NOUS_SCORER_MERGER_LOGINS is empty, so PR checks are not "
                "verified"
            )
        if agent_id is None:
            agent_id = settings.agent_id
        now = datetime.now(UTC)
        try:
            await run_scoring_pass(database, agent_id, window_days=window_days, now=now, pr_verifier=verifier)
        except Exception as exc:
            await _record_failed_pass(database, agent_id, exc)
            raise
    finally:
        if reader is not None:
            try:
                await reader.aclose()
            except Exception:
                logger.warning("Task scores: closing the GitHub client failed after the pass", exc_info=True)
        try:
            await database.disconnect()
        except Exception:
            # Closing the pool is not the pass: it neither fails a pass that committed nor hides a pass's own error.
            logger.warning("Task scores: the database disconnect failed after the pass", exc_info=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The command. 0 when the pass committed its rows (a failed disconnect afterwards is only logged), 1 when
    anything else failed."""
    parser = argparse.ArgumentParser(
        prog="python -m nous.observability.task_score_job", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--agent-id", default=None, help="the agent to score (default: NOUS_AGENT_ID)")
    parser.add_argument(
        "--window-days", type=int, default=SCORING_WINDOW_DAYS, help="days of finished tasks to re-score"
    )
    args = parser.parse_args(argv)
    configure_logging(logging.INFO)
    try:
        return asyncio.run(_run(args.agent_id, args.window_days))
    except Exception:
        logger.exception("Task scores: the scoring pass failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
