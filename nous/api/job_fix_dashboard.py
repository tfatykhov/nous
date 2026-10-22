"""The ``job_fixes`` section of GET /dashboard/autonomy: every recurring job check-and-fix has a record for.

Read-only, from ``schedule.metadata["job_fix"]`` (a JSONB key, so Postgres only, apart from the ORM selects of
``autonomy_dashboard``) plus, for a job whose change is being judged, the fixer's own read of its runs since the
change, so the panel and the judge count the same runs. Unlike the fix brief, the owner report and the undo
journal, this screen is read by the owner only, so it may show the score-derived counts (the baseline and the
judging progress). The detail texts are written by the checking run; they are clipped here and never rendered as
markup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nous.handlers.job_fixes import (
    BASELINE_RUNS,
    JUDGE_AFTER_RUNS,
    MAX_NEW_INSTRUCTIONS_CHARS,
    baseline_ok,
    parse_time,
)
from nous.handlers.job_lessons import record_head
from nous.heart.job_runs import FIX_KEY

if TYPE_CHECKING:
    from nous.heart import Heart
    from nous.storage.models import Schedule

LIVE_STATES = ("running", "judging", "stopping")  # the fixer still has a step to take now
TEXT_CHARS = MAX_NEW_INSTRUCTIONS_CHARS  # clip for each text the panel shows in full
DETAIL_KEYS = ("why", "what_changed", "diagnosis", "owner_action", "checked", "reason", "new_instructions")
CHECK_KEYS = ("status", "outcome", "head")


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _text(value: object) -> str | None:
    """A non-empty string clipped to TEXT_CHARS, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:TEXT_CHARS] + "..." if len(value) > TEXT_CHARS else value


def _when(value: object) -> str | None:
    when = parse_time(value)
    return when.isoformat() if when is not None else None


def _detail(record: dict) -> dict[str, Any]:
    """The check's own words from the record: its verdict fields, a refusal reason and how the check run ended."""
    raw = record.get("detail") if isinstance(record.get("detail"), dict) else {}
    detail: dict[str, Any] = {k: v for k in DETAIL_KEYS if (v := _text(raw.get(k))) is not None}
    check = raw.get("check")
    if isinstance(check, dict):
        detail["check"] = {k: v for k in CHECK_KEYS if (v := _text(check.get(k))) is not None}
    return detail


async def _row(heart: Heart, schedule: Schedule, record: dict) -> dict[str, Any]:
    state = _str(record.get("state"))
    old_task = _str(record.get("old_task"))
    changed = old_task is not None and old_task != schedule.task
    baseline = record.get("baseline")
    baseline = {"bad": baseline["bad"], "scored": baseline["scored"]} if baseline_ok(baseline) else None
    applied = parse_time(record.get("applied_at"))
    since_change = None
    if state == "judging" and applied is not None and baseline is not None:  # the judge skips a record without them
        # The judge's own read (JobFixer._advance_judging): provisional scores too, after the change.
        runs = await heart.job_runs.scored_runs(schedule.id.hex, after=applied, limit=BASELINE_RUNS, settled_only=False)
        since_change = {"scored": len(runs), "bad": sum(1 for r in runs if r.bad), "needed": JUDGE_AFTER_RUNS}
    return {
        "schedule_id": schedule.id.hex,
        "job": record_head(old_task or schedule.task),
        "active": schedule.active,
        "state": state,
        "trigger": _str(record.get("trigger")),
        "verdict": _str(record.get("verdict")),
        "stop_reason": _str(record.get("stop_reason")),
        "started_at": _when(record.get("started_at")),
        "applied_at": applied.isoformat() if applied is not None else None,
        "settled_at": _when(record.get("settled_at")),
        "stopped_at": _when(record.get("stopped_at")),
        "fix_run": _str(record.get("fix_run")),
        "reported": _str(record.get("report_id")) is not None,
        "baseline": baseline,
        "since_change": since_change,
        "detail": _detail(record),
        # Only while a changed text stands (judging or kept): what the instructions were and what they are now.
        # A stop restores the old text, so then both would be the same.
        "old_task": _text(old_task) if changed else None,
        "task": _text(schedule.task) if changed else None,
    }


_NEVER = datetime.min.replace(tzinfo=UTC)


def _latest(row: dict[str, Any]) -> datetime:
    """The row's newest time."""
    keys = ("stopped_at", "settled_at", "applied_at", "started_at")
    times = [datetime.fromisoformat(row[k]) for k in keys if row[k]]
    return max(times, default=_NEVER)


async def get_job_fix_data(heart: Heart, *, enabled: bool) -> dict[str, Any]:
    """Every check-and-fix record of this agent, whatever the window: live ones first, then newest first.

    ``enabled`` is ``NOUS_JOB_FIXES_ENABLED``: with it off nothing new starts, but older records still show.
    """
    rows = []
    for schedule in await heart.schedules.list_with_metadata_key(FIX_KEY):
        record = (schedule.metadata_ or {}).get(FIX_KEY)
        if isinstance(record, dict):
            rows.append(await _row(heart, schedule, record))
    rows.sort(key=_latest, reverse=True)
    rows.sort(key=lambda r: r["state"] not in LIVE_STATES)  # stable: live first, each part still newest first
    return {"enabled": enabled, "jobs": rows}
