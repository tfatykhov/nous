"""Read-only access to the runs of recurring background jobs.

A job is a recurring ``heart.schedules`` row. Each fire is a ``heart.subtasks`` row whose
``metadata.schedule_id`` is the schedule's uuid hex (32 characters, no dashes). The scorer writes
``scoring.task_scores`` for finished subtasks; this reader joins the three to find a job's runs.

The score only decides *which* runs come back (``JobRun.bad``). Every other field of a
:class:`JobRun` is the run's own record, so a caller can render a run without rendering a score.
Runs of a fix attempt (subtask metadata flag :data:`FIX_KEY`) are never returned.
Lesson reads (``lesson_cases``, ``good_runs``, ``scored_runs``) use settled scores only, unless ``scored_runs`` is
called with ``settled_only=False``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nous.observability.task_scoring import OUTCOMES, SCORER_VERSION
from nous.storage.database import Database

logger = logging.getLogger(__name__)

BAD_OUTCOMES: tuple[str, ...] = ("failed", "partial")
GOOD_OUTCOMES: tuple[str, ...] = tuple(o for o in OUTCOMES if o.startswith("done_"))
LESSONS_KEY = "job_lessons"  # schedule metadata key: {"reviewed_until": iso, "retry": {"run": str, "n": int}}
FIX_KEY = "job_fix"  # schedule metadata key written by check-and-fix; subtask metadata flag on fix runs

_BAD_SQL = ", ".join(f"'{o}'" for o in BAD_OUTCOMES)
_GOOD_SQL = ", ".join(f"'{o}'" for o in GOOD_OUTCOMES)

_COLUMNS = """
 s.id AS subtask_id, replace(CAST(sch.id AS text), '-', '') AS schedule_id, ts.finished_at, ts.outcome,
       s.status, s.final_outcome, s.report_jsonb->>'blocked_reason' AS blocked_reason, s.error, s.result
"""
_SELECT = "SELECT" + _COLUMNS

# Every identifier is agent-scoped; ids are compared as text (never a ::uuid cast of subtask metadata, which
# may hold anything).
_FROM = """
FROM scoring.task_scores ts
JOIN heart.subtasks s ON ts.task_id = CAST(s.id AS text) AND s.agent_id = :agent_id
JOIN heart.schedules sch ON s.metadata->>'schedule_id' = replace(CAST(sch.id AS text), '-', '')
     AND sch.agent_id = :agent_id
WHERE ts.agent_id = :agent_id AND ts.task_kind = 'subtask' AND ts.scorer_version = :v
  AND (s.metadata -> CAST(:fix_key AS text)) IS NULL
"""

# Lesson reads rest on final labels: a provisional (unsettled) score may still be rewritten by the scorer.
_SETTLED = " AND ts.settled"

_ACTIVE_BAD = f" AND sch.active AND sch.schedule_type = 'recurring' AND ts.outcome IN ({_BAD_SQL})"


@dataclass(frozen=True, slots=True)
class JobRun:
    """One finished run of a job: its own record plus whether the score picked it as a bad run."""

    subtask_id: UUID
    schedule_id: str
    finished_at: datetime
    bad: bool
    status: str
    final_outcome: str | None
    blocked_reason: str | None
    error: str | None
    result: str | None


def _run(row: Mapping[str, Any]) -> JobRun:
    return JobRun(
        subtask_id=row["subtask_id"],
        schedule_id=row["schedule_id"],
        finished_at=row["finished_at"],
        bad=row["outcome"] in BAD_OUTCOMES,
        status=row["status"],
        final_outcome=row["final_outcome"],
        blocked_reason=row["blocked_reason"],
        error=row["error"],
        result=row["result"],
    )


def _reviewed(sch_meta: object, finished_at: datetime) -> bool:
    """True when the schedule's ``reviewed_until`` covers ``finished_at``; malformed metadata is "not reviewed"."""
    if not isinstance(sch_meta, dict):
        return False
    lessons = sch_meta.get(LESSONS_KEY)
    if not isinstance(lessons, dict):
        return False
    raw = lessons.get("reviewed_until")
    if not isinstance(raw, str):
        return False
    try:
        until = datetime.fromisoformat(raw)
        if until.tzinfo is None:
            return False
        return until >= finished_at
    except (TypeError, ValueError):
        return False


class JobRunReader:
    """Finds a recurring job's runs by score. Read-only, agent-scoped, scorer-version-scoped."""

    def __init__(self, database: Database, agent_id: str, scorer_version: int = SCORER_VERSION) -> None:
        self._db = database
        self._agent_id = agent_id
        self._version = scorer_version

    def _params(self, **extra: Any) -> dict[str, Any]:
        return {"agent_id": self._agent_id, "v": self._version, "fix_key": FIX_KEY, **extra}

    async def _rows(self, sql: str, params: dict[str, Any]) -> list[Mapping[str, Any]]:
        async with self._db.session() as session:
            result = await session.execute(text(sql), params)
            return list(result.mappings().all())

    async def lesson_cases(self, *, since: datetime, limit: int | None = None) -> list[JobRun]:
        """The newest bad run of each active recurring job since ``since``, newest first, at most ``limit`` (None: all).

        A job whose ``job_lessons.reviewed_until`` is at or after that run's finish is skipped. Settled scores only.
        """
        sql = (
            "SELECT DISTINCT ON (sch.id)" + _COLUMNS + ", sch.metadata AS sch_meta" + _FROM + _SETTLED + _ACTIVE_BAD
            + " AND ts.finished_at >= :since ORDER BY sch.id, ts.finished_at DESC"
        )
        rows = await self._rows(sql, self._params(since=since))
        kept = [r for r in rows if not _reviewed(r["sch_meta"], r["finished_at"])]
        kept.sort(key=lambda r: r["finished_at"], reverse=True)
        return [_run(r) for r in (kept if limit is None else kept[:limit])]

    async def good_runs(self, schedule_id: str, *, before: datetime, since: datetime, limit: int = 3) -> list[JobRun]:
        """Good runs of one job in ``[since, before)``, newest first."""
        sql = (
            _SELECT + _FROM + _SETTLED
            + f" AND s.metadata->>'schedule_id' = :schedule_id AND ts.outcome IN ({_GOOD_SQL})"
            + " AND ts.finished_at >= :since AND ts.finished_at < :before ORDER BY ts.finished_at DESC LIMIT :limit"
        )
        rows = await self._rows(sql, self._params(schedule_id=schedule_id, since=since, before=before, limit=limit))
        return [_run(r) for r in rows]

    async def scored_runs(
        self,
        schedule_id: str,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
        settled_only: bool = True,
    ) -> list[JobRun]:
        """Scored runs of one job (``not_scored`` left out), newest first; ``after`` exclusive, ``until`` inclusive.

        Lesson reads use settled scores only; pass ``settled_only=False`` to include provisional ones.
        """
        sql = (
            _SELECT + _FROM + (_SETTLED if settled_only else "")
            + " AND s.metadata->>'schedule_id' = :schedule_id AND ts.outcome <> 'not_scored'"
            + " AND (CAST(:after AS timestamptz) IS NULL OR ts.finished_at > CAST(:after AS timestamptz))"
            + " AND (CAST(:until AS timestamptz) IS NULL OR ts.finished_at <= CAST(:until AS timestamptz))"
            + " ORDER BY ts.finished_at DESC LIMIT :limit"
        )
        rows = await self._rows(sql, self._params(schedule_id=schedule_id, after=after, until=until, limit=limit))
        return [_run(r) for r in rows]

    async def ledger_calls(
        self, subtask_ids: Sequence[UUID], *, per_run: int = 30
    ) -> dict[UUID, list[tuple[str, str]]]:
        """``(tool_name, status)`` of each run's ledger entries, oldest first, the first ``per_run`` kept.

        Every requested id is present in the result (an empty list when it has no entries).
        """
        out: dict[UUID, list[tuple[str, str]]] = {sid: [] for sid in subtask_ids}
        if not out:
            return out
        sql = (
            "SELECT subtask_id, tool_name, status FROM nous_system.execution_ledger "
            "WHERE agent_id = :agent_id AND subtask_id = ANY(CAST(:ids AS uuid[])) ORDER BY created_at, id"
        )
        async with self._db.session() as session:
            result = await session.execute(
                text(sql), {"agent_id": self._agent_id, "ids": [str(sid) for sid in out]}
            )
            rows = result.all()
        for sid, tool, status in rows:
            calls = out.get(sid)
            if calls is not None and len(calls) < per_run:
                calls.append((tool, status))
        return out

    async def schedules_with_bad_runs(self, *, since: datetime) -> list[str]:
        """Hex ids of active recurring jobs with a bad run since ``since``, newest bad run first.

        Provisional scores count too; the order is each job's newest bad run, newest first.
        """
        sql = (
            "SELECT replace(CAST(sch.id AS text), '-', '') AS schedule_id" + _FROM + _ACTIVE_BAD
            + " AND ts.finished_at >= :since GROUP BY sch.id ORDER BY max(ts.finished_at) DESC, sch.id"
        )
        rows = await self._rows(sql, self._params(since=since))
        return [r["schedule_id"] for r in rows]
