"""F118: the reads and writes of ``nous_system.judge_verdicts`` (migration 121), in plain SQL.

Written by the outside judge's worker only, one row per subject, judge name and judge version. A second write for a
subject is a no-op, except over an ``error`` or ``timeout`` row with fewer than ``MAX_ATTEMPTS`` attempts: that row is
replaced by the new attempt, its calls and known token counts added and its attempts counted. Read by the worker
(whether a subject is judged, today's call count) and by ``GET /dashboard/outside-judge``. Every statement is filtered
on the agent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The most attempts at one subject at one judge version: an error or timeout row is tried again until then.
MAX_ATTEMPTS = 3
# The statuses of a row that a later attempt replaces.
RETRIED_STATUSES = ("error", "timeout")
# A retried row's tokens: the sum of the attempts' known counts. An attempt whose count is unknown adds nothing, and
# the sum is unknown (NULL) only while no attempt's count is known.
_TOKEN_SUM = (
    "CASE WHEN v.{column} IS NULL AND EXCLUDED.{column} IS NULL THEN NULL "
    "ELSE COALESCE(v.{column}, 0) + COALESCE(EXCLUDED.{column}, 0) END"
)

_INSERT = text(
    "INSERT INTO nous_system.judge_verdicts AS v (agent_id, subject_kind, subject_id, judge_name, judge_version, "
    "model, status, verdict, confidence, labels, reason, against_grader, against_grade, claim_at, evidence, "
    "tokens_in, tokens_out, latency_ms, calls, attempts) VALUES (:agent_id, :subject_kind, :subject_id, :judge_name, "
    ":judge_version, :model, :status, :verdict, :confidence, CAST(:labels AS jsonb), :reason, :against_grader, "
    ":against_grade, :claim_at, CAST(:evidence AS jsonb), :tokens_in, :tokens_out, :latency_ms, :calls, 1) "
    "ON CONFLICT ON CONSTRAINT uq_judge_verdicts_subject DO UPDATE SET model = EXCLUDED.model, "
    "status = EXCLUDED.status, verdict = EXCLUDED.verdict, confidence = EXCLUDED.confidence, labels = EXCLUDED.labels, "
    "reason = EXCLUDED.reason, against_grader = EXCLUDED.against_grader, against_grade = EXCLUDED.against_grade, "
    "claim_at = EXCLUDED.claim_at, "
    "evidence = EXCLUDED.evidence, "
    f"tokens_in = {_TOKEN_SUM.format(column='tokens_in')}, tokens_out = {_TOKEN_SUM.format(column='tokens_out')}, "
    "latency_ms = EXCLUDED.latency_ms, calls = v.calls + EXCLUDED.calls, attempts = v.attempts + 1, "
    "judged_at = now() "
    f"WHERE v.status IN {RETRIED_STATUSES!r} AND v.attempts < {MAX_ATTEMPTS}"
)
_JUDGED = text(
    "SELECT 1 FROM nous_system.judge_verdicts WHERE agent_id = :agent AND subject_kind = :kind "
    "AND subject_id = :subject AND judge_name = :name AND judge_version = :version "
    f"AND (status NOT IN {RETRIED_STATUSES!r} OR attempts >= {MAX_ATTEMPTS})"
)
# A row tried again counts all its calls on the day of its last attempt.
_CALLS_TODAY = (
    "SELECT COALESCE(SUM(calls), 0) FROM nous_system.judge_verdicts WHERE agent_id = :agent AND judged_at >= :since"
)
_CALLS_TODAY_ALL = text(_CALLS_TODAY)
_CALLS_TODAY_OF_JUDGE = text(_CALLS_TODAY + " AND judge_name = :judge")


async def insert_verdict(session: AsyncSession, row: dict[str, Any]) -> bool:
    """Write one verdict row (``labels`` a list, ``evidence`` a dict, ``calls`` the model requests sent, ``claim_at``
    when the judged claim was fixed) and commit: a new row, or the next attempt over an ``error`` or ``timeout`` row,
    which keeps that attempt's claim and its time. False when the subject's row is final."""
    params = {**row, "labels": json.dumps(list(row["labels"])), "evidence": json.dumps(row["evidence"])}
    result = await session.execute(_INSERT, params)
    await session.commit()
    return bool(result.rowcount)


async def already_judged(
    session: AsyncSession, agent_id: str, kind: str, subject_id: UUID, judge_name: str, judge_version: str
) -> bool:
    """Whether the subject's row at this judge name and version is final: any status but ``error`` and ``timeout``,
    or one of those after ``MAX_ATTEMPTS`` attempts."""
    params = {"agent": agent_id, "kind": kind, "subject": subject_id, "name": judge_name, "version": judge_version}
    return (await session.execute(_JUDGED, params)).first() is not None


def start_of_utc_day(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def calls_today(session: AsyncSession, agent_id: str, now: datetime, judge_name: str | None = None) -> int:
    """The model requests of this agent's judge ``judge_name`` (every judge when None) since the start of ``now``'s
    UTC day, at any version (a row written without a call counts none). The daily limit is per judge."""
    params = {"agent": agent_id, "since": start_of_utc_day(now)}
    if judge_name is None:
        return int(await session.scalar(_CALLS_TODAY_ALL, params) or 0)
    return int(await session.scalar(_CALLS_TODAY_OF_JUDGE, {**params, "judge": judge_name}) or 0)


# ---- the numbers (GET /dashboard/outside-judge) -------------------------------------------------------------------

DAYS_DEFAULT = 14
DAYS_MAX = 90
DISAGREEMENTS_MAX = 50
# The order of the graded outcomes: a verdict below the claim is harsher, above it kinder.
_RANK = {"failure": 0, "partial": 1, "success": 2}

_WINDOW_ROWS = text(
    "SELECT subject_id, status, verdict, confidence, labels, reason, against_grader, against_grade, tokens_in, "
    "tokens_out, latency_ms, calls, judged_at FROM nous_system.judge_verdicts "
    "WHERE agent_id = :agent AND subject_kind = 'decision' AND judge_name = :name AND judge_version = :version "
    "AND judged_at >= :since"
)


def parse_days(raw: str | None) -> int:
    """The ``days`` query parameter: a whole number from 1 to 90, 14 when absent. Raises ValueError otherwise."""
    if raw is None or raw == "":
        return DAYS_DEFAULT
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= DAYS_MAX:
        raise ValueError(f"days must be a whole number from 1 to {DAYS_MAX}")
    return int(raw)


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The numbers of a set of verdict rows (dicts with the columns of ``_WINDOW_ROWS``): ``decisions``, ``cost``
    without today's figures, and ``disagreements``.

    Agreement uses ``ok`` rows only, grouped by ``against_grader`` as stored: ``agree`` when the verdict is the claimed
    grade, ``cannot_tell`` apart and left out of ``agree_rate``, and with ``failure < partial < success`` a verdict
    below the grade is ``judge_harsher`` and above it ``judge_kinder``. Calls are the model requests the rows record;
    tokens, the average per verdict and the median latency are over the rows that made a call, and input tokens are
    every kind (uncached, read from the cache, written to it). A disagreement is an ``ok`` verdict other than
    ``cannot_tell`` that differs from the grade; the latest 50 are listed with ids, labels and the judge's sentence
    only."""
    ok = [row for row in rows if row["status"] == "ok"]
    called = [row for row in rows if (row["calls"] or 0) > 0]
    by_verdict: dict[str, int] = {}
    by_grader: dict[str, dict[str, Any]] = {}
    for row in ok:
        by_verdict[row["verdict"]] = by_verdict.get(row["verdict"], 0) + 1
        group = by_grader.setdefault(
            str(row["against_grader"]),
            {"n": 0, "agree": 0, "judge_harsher": 0, "judge_kinder": 0, "cannot_tell": 0, "agree_rate": None},
        )
        group["n"] += 1
        verdict, grade = row["verdict"], row["against_grade"]
        if verdict == "cannot_tell":
            group["cannot_tell"] += 1
        elif verdict == grade:
            group["agree"] += 1
        elif verdict in _RANK and grade in _RANK:
            group["judge_harsher" if _RANK[verdict] < _RANK[grade] else "judge_kinder"] += 1
    for group in by_grader.values():
        graded = group["n"] - group["cannot_tell"]
        group["agree_rate"] = round(group["agree"] / graded, 4) if graded else None
    skipped_by_label: dict[str, int] = {}
    for row in rows:
        if row["status"] == "skipped":
            for label in row["labels"] or []:
                skipped_by_label[label] = skipped_by_label.get(label, 0) + 1
    tokens_in = sum(row["tokens_in"] or 0 for row in called)
    tokens_out = sum(row["tokens_out"] or 0 for row in called)
    disagreements = [
        {
            "subject_id": str(row["subject_id"]),
            "against_grader": row["against_grader"],
            "against_grade": row["against_grade"],
            "verdict": row["verdict"],
            "confidence": row["confidence"],
            "labels": list(row["labels"] or []),
            "reason": row["reason"],
            "judged_at": row["judged_at"].isoformat(),
        }
        for row in sorted(ok, key=lambda r: r["judged_at"], reverse=True)
        if row["verdict"] != "cannot_tell" and row["verdict"] != row["against_grade"]
    ][:DISAGREEMENTS_MAX]
    return {
        "decisions": {
            "judged": len(ok),
            "by_verdict": by_verdict,
            "skipped_by_label": skipped_by_label,
            "errors": sum(1 for row in rows if row["status"] in ("error", "timeout")),
            "by_grader": by_grader,
        },
        "cost": {
            "calls": sum(row["calls"] or 0 for row in called),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_per_verdict_avg": round((tokens_in + tokens_out) / len(called), 1) if called else None,
            "latency_ms_p50": _median([row["latency_ms"] for row in called if row["latency_ms"] is not None]),
        },
        "disagreements": disagreements,
    }


async def get_numbers(
    session: AsyncSession,
    agent_id: str,
    *,
    days: int,
    judge_name: str,
    judge_version: str,
    cap: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``summarize`` over this agent's verdicts of one judge name and version from the last ``days`` days, with
    today's calls of that judge (every version: the daily limit is per judge) and the limit."""
    now = now or datetime.now(UTC)
    params = {"agent": agent_id, "name": judge_name, "version": judge_version, "since": now - timedelta(days=days)}
    rows = [dict(row._mapping) for row in await session.execute(_WINDOW_ROWS, params)]
    numbers = summarize(rows)
    numbers["cost"]["today"] = {"calls": await calls_today(session, agent_id, now, judge_name), "cap": cap}
    return numbers
