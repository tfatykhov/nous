"""F118: the reads and writes of ``nous_system.judge_verdicts`` (migration 121), in plain SQL.

Written by the outside judge's worker only, one row per subject, judge name and judge version. A second write for a
subject is a no-op, except over an ``error`` or ``timeout`` row with fewer than ``MAX_ATTEMPTS`` attempts: that row is
replaced by the new attempt, its calls and known token counts added and its attempts counted. Read by the worker
(whether a subject is judged, today's call count) and by ``GET /dashboard/outside-judge``. Every statement is filtered
on the agent.

With finished task reports judged (``NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED``), the sweep reads from here which decisions
and reports have no final row yet (it reads ``brain.decisions`` and ``heart.subtasks`` and writes neither), and the
route reads the report numbers, beside the finish checks' verdicts and the owner's spot checks.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
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


# ---- two judges on the same decisions ------------------------------------------------------------------------------

PAIR_DISAGREEMENTS_MAX = 20

# Both judges' ``ok`` rows on one subject (the unique key leaves at most one row per subject and judge version), both
# judged in the window; whether they were given the same claim (the same ``claim_at``) and read the same evidence
# counts.
_PAIR_ROWS = text(
    "SELECT a.subject_id, a.claim_at, a.against_grader, a.against_grade, a.verdict AS first_verdict, "
    "b.verdict AS second_verdict, a.confidence AS first_confidence, b.confidence AS second_confidence, "
    "a.labels AS first_labels, b.labels AS second_labels, a.claim_at = b.claim_at AS same_claim, "
    "a.evidence = b.evidence AS same_evidence "
    "FROM nous_system.judge_verdicts a JOIN nous_system.judge_verdicts b ON b.agent_id = a.agent_id "
    "AND b.subject_kind = a.subject_kind AND b.subject_id = a.subject_id "
    "WHERE a.agent_id = :agent AND a.subject_kind = 'decision' AND a.judge_name = :first_name "
    "AND a.judge_version = :first_version AND b.judge_name = :second_name AND b.judge_version = :second_version "
    "AND a.status = 'ok' AND b.status = 'ok' AND a.judged_at >= :since AND b.judged_at >= :since"
)


def summarize_pairs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How two judges' verdicts compare (rows with the columns of ``_PAIR_ROWS``).

    A pair is both judges' ``ok`` verdicts on the same claim of a subject (the same ``claim_at``); a subject whose two
    verdicts were given on different claims is counted in ``other_claim`` only. ``agree`` counts pairs with the same
    verdict, neither ``cannot_tell``; ``either_cannot_tell`` the pairs where one or both said ``cannot_tell``;
    ``agree_rate`` is ``agree`` over the pairs without ``cannot_tell``. ``matrix`` counts the pairs by the first judge's
    verdict, then the second's. ``evidence_differs`` counts pairs whose two evidence counts differ (each judge builds
    its record itself). The latest 20 pairs with different verdicts, neither ``cannot_tell``, are listed, latest claim
    first, with ids, grades, both verdicts, confidences and labels."""
    pairs = [row for row in rows if row["same_claim"]]
    matrix: dict[str, dict[str, int]] = {}
    agree = either_cannot_tell = 0
    for row in pairs:
        first, second = row["first_verdict"], row["second_verdict"]
        matrix.setdefault(first, {})
        matrix[first][second] = matrix[first].get(second, 0) + 1
        if "cannot_tell" in (first, second):
            either_cannot_tell += 1
        elif first == second:
            agree += 1
    graded = len(pairs) - either_cannot_tell
    ordered = sorted(pairs, key=lambda row: (row["claim_at"], str(row["subject_id"])), reverse=True)
    disagreements = [
        {
            "subject_id": str(row["subject_id"]),
            "claim_at": row["claim_at"].isoformat(),
            "against_grader": row["against_grader"],
            "against_grade": row["against_grade"],
            "first_verdict": row["first_verdict"],
            "second_verdict": row["second_verdict"],
            "first_confidence": row["first_confidence"],
            "second_confidence": row["second_confidence"],
            "first_labels": list(row["first_labels"] or []),
            "second_labels": list(row["second_labels"] or []),
            "evidence_differs": not row["same_evidence"],
        }
        for row in ordered
        if "cannot_tell" not in (row["first_verdict"], row["second_verdict"])
        and row["first_verdict"] != row["second_verdict"]
    ][:PAIR_DISAGREEMENTS_MAX]
    return {
        "pairs": len(pairs),
        "agree": agree,
        "either_cannot_tell": either_cannot_tell,
        "agree_rate": round(agree / graded, 4) if graded else None,
        "matrix": matrix,
        "evidence_differs": sum(1 for row in pairs if not row["same_evidence"]),
        "other_claim": len(rows) - len(pairs),
        "disagreements": disagreements,
    }


async def pair_numbers(
    session: AsyncSession,
    agent_id: str,
    *,
    days: int,
    first: tuple[str, str],
    second: tuple[str, str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """``summarize_pairs`` over this agent's decision verdicts of two judges (each a name and a version) from the last
    ``days`` days."""
    now = now or datetime.now(UTC)
    params = {
        "agent": agent_id,
        "first_name": first[0],
        "first_version": first[1],
        "second_name": second[0],
        "second_version": second[1],
        "since": now - timedelta(days=days),
    }
    return summarize_pairs([dict(row._mapping) for row in await session.execute(_PAIR_ROWS, params)])


# ---- the hourly sweep (NOUS_OUTSIDE_JUDGE_REPORTS_ENABLED) -------------------------------------------------------

# How far back the sweep looks: decisions graded by the owner or a fixed signal, decisions the agent resolved, reports.
SWEEP_DECISION_DAYS = 30
SWEEP_AGENT_DAYS = 7
SWEEP_REPORT_DAYS = 2
# The graders whose decisions the sweep takes (any other, such as a background turn's, is not taken).
OWNER_GRADERS = ("a2ui", "external")  # a companion tap, the owner-guarded review route
AGENT_GRADER = "agent"  # the agent in an interactive turn: what a restart lost
SIGNAL_GRADER = "auto"  # the decision reviewer's fixed signals
# The decision reviewer also grades failure from the low confidence the agent itself stated when it recorded the
# decision (``ErrorSignal`` in nous/handlers/decision_reviewer.py, whose note starts with this text). That grade is not
# an outcome: it is stored under its own grader name and kept out of the agreement with outcome grades.
STATED_CONFIDENCE_GRADER = "auto:stated_confidence"
STATED_CONFIDENCE_NOTE = "Low confidence ("

# A verdict row the judge will not try again (the rule of ``_JUDGED``).
_FINAL_ROW = f"(v.status NOT IN {RETRIED_STATUSES!r} OR v.attempts >= {MAX_ATTEMPTS})"
_SWEEP_DECISIONS = text(
    "SELECT d.id, d.reviewer, d.outcome, d.outcome_result, d.reviewed_at FROM brain.decisions d "
    "WHERE d.agent_id = :agent AND d.outcome IN ('success', 'partial', 'failure') "
    "AND (d.outcome <> 'failure' OR d.confidence IS DISTINCT FROM 0.0) AND d.reviewed_at >= :since "
    "AND (d.reviewer IN :graders OR (d.reviewer = :agent_grader AND d.reviewed_at >= :agent_since)) "
    "AND NOT EXISTS (SELECT 1 FROM nous_system.judge_verdicts v WHERE v.agent_id = d.agent_id "
    "AND v.subject_kind = 'decision' AND v.subject_id = d.id AND v.judge_name = :name "
    f"AND v.judge_version = :version AND {_FINAL_ROW}) "
    "ORDER BY CASE WHEN d.reviewer IN :owners THEN 0 WHEN d.reviewer = :agent_grader THEN 1 ELSE 2 END, "
    "d.reviewed_at DESC, d.id LIMIT :limit"
).bindparams(bindparam("graders", expanding=True), bindparam("owners", expanding=True))
_SWEEP_REPORTS = text(
    "SELECT s.id, s.completed_at FROM heart.subtasks s WHERE s.agent_id = :agent AND s.status = 'completed' "
    "AND s.final_outcome = 'completed' AND s.completed_at >= :since "
    "AND COALESCE(s.report_jsonb ->> 'summary', '') <> '' "
    "AND NOT EXISTS (SELECT 1 FROM nous_system.judge_verdicts v WHERE v.agent_id = s.agent_id "
    "AND v.subject_kind = 'task_report' AND v.subject_id = s.id AND v.judge_name = :name "
    f"AND v.judge_version = :version AND {_FINAL_ROW}) "
    "ORDER BY s.completed_at DESC, s.id LIMIT :limit"
)


def grader_of(reviewer: str, outcome: str, note: str | None) -> str:
    """The grader a swept decision's claim is stored under: its reviewer, except a reviewer's grade from the agent's
    own low stated confidence, which is ``STATED_CONFIDENCE_GRADER``."""
    if reviewer == SIGNAL_GRADER and outcome == "failure" and (note or "").startswith(STATED_CONFIDENCE_NOTE):
        return STATED_CONFIDENCE_GRADER
    return reviewer


async def sweep_decisions(
    session: AsyncSession, agent_id: str, *, judge_name: str, judge_version: str, now: datetime, limit: int
) -> list[dict[str, Any]]:
    """Up to ``limit`` of this agent's decisions graded success, partial or failure (not the abandoned mark) without a
    final row at this judge name and version, in the sweep's order: graded by the owner in the last 30 days, by the
    agent in an interactive turn in the last 7, by a fixed signal in the last 30; newest first in each. Each is a dict
    of ``id``, ``grader`` (``grader_of``), ``grade``, ``note`` and ``graded_at``. Read-only."""
    params = {
        "agent": agent_id,
        "since": now - timedelta(days=SWEEP_DECISION_DAYS),
        "agent_since": now - timedelta(days=SWEEP_AGENT_DAYS),
        "graders": [*OWNER_GRADERS, SIGNAL_GRADER],
        "owners": list(OWNER_GRADERS),
        "agent_grader": AGENT_GRADER,
        "name": judge_name,
        "version": judge_version,
        "limit": limit,
    }
    return [
        {
            "id": row.id,
            "grader": grader_of(row.reviewer, row.outcome, row.outcome_result),
            "grade": row.outcome,
            "note": row.outcome_result,
            "graded_at": row.reviewed_at,
        }
        for row in await session.execute(_SWEEP_DECISIONS, params)
    ]


async def sweep_reports(
    session: AsyncSession, agent_id: str, *, judge_name: str, judge_version: str, now: datetime, limit: int
) -> list[dict[str, Any]]:
    """Up to ``limit`` of this agent's tasks finished as completed with a report in the last 2 days, without a final
    row at this judge name and version, newest first: dicts of ``id`` and ``completed_at``. Read-only."""
    params = {
        "agent": agent_id,
        "since": now - timedelta(days=SWEEP_REPORT_DAYS),
        "name": judge_name,
        "version": judge_version,
        "limit": limit,
    }
    return [
        {"id": row.id, "completed_at": row.completed_at} for row in await session.execute(_SWEEP_REPORTS, params)
    ]
