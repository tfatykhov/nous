"""The ``guards`` section of GET /dashboard/autonomy (F107 phase 2): what the scheduled-run guards did.

Read-only, and only from the database (``heart.schedules``, ``heart.subtasks``, ``nous_system.events``) and
Settings, so it shows what was persisted, never what one process happens to hold in memory. JSONB key reads,
so Postgres only, like ``job_fix_dashboard``. A guard run leaves one record: ``report_jsonb.guard`` on a row it
skipped, otherwise a ``subtask_guard_ran`` event; a guard naming no allowlisted command leaves a
``subtask_guard_unresolved`` event, and the turn of a shadow run a ``subtask_guard_acted`` event. Record texts
(command, stderr head, job) are returned as written; the view renders them as plain text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.dag.approval import as_utc
from nous.handlers.schedule_guard import MODE_SHADOW, MODE_SKIP, REASONS
from nous.storage.models import Event, Schedule, Subtask

GUARD_KEY = "guard"
RAN, UNRESOLVED, ACTED = "subtask_guard_ran", "subtask_guard_unresolved", "subtask_guard_acted"
SIGNALS = ("report", "finding", "inbox", "push", "ledger")  # schedule_guard.acted_signals, in its order
RECENT_LIMIT = 10
JOB_CHARS = 120
RECORD_KEYS = (
    "name", "mode", "reason", "exit_code", "timed_out", "duration_ms", "stdout_bytes", "stderr_bytes",
    "stderr_head", "command",
)  # fmt: skip


def _hex(value: object) -> str | None:
    return value.replace("-", "").lower() if isinstance(value, str) and value else None


def _job(task: str) -> str:
    """The first non-empty line of the job's text, clipped."""
    line = next((ln.strip() for ln in (task or "").splitlines() if ln.strip()), "")
    return line[:JOB_CHARS] + "…" if len(line) > JOB_CHARS else line


def _reason(guard: dict[str, Any]) -> str | None:
    """A record's fall-through reason. A record from before phase 2 has none: derived from its fields."""
    if "reason" in guard:
        return guard["reason"] if guard["reason"] in REASONS else ("error" if guard["reason"] else None)
    if guard.get("error"):
        return "error"
    if guard.get("timed_out"):
        return "timeout"
    if guard.get("exit_code") != 0:
        return "exit"
    if guard.get("stdout_bytes"):
        return "output"
    return "stderr" if guard.get("stderr_bytes") else "error"


def _classify(guard: dict[str, Any], ran: bool) -> tuple[str, str | None]:
    """``(kind, reason)`` of one record: ``skipped``, ``would_skip`` (shadow) or ``fell_through`` with its reason."""
    if not ran:
        return "fell_through", "not_allowlisted"
    if guard.get("skipped") is True:
        return "skipped", None
    reason = _reason(guard)
    if reason is None and guard.get("mode") == MODE_SHADOW:
        return "would_skip", None
    return "fell_through", reason or "error"


def _schedule_row(schedule: Schedule, commands: dict[str, str]) -> dict[str, Any]:
    raw = (schedule.metadata_ or {}).get(GUARD_KEY)
    raw = raw if isinstance(raw, dict) else {}
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    return {
        "schedule_id": schedule.id.hex,
        "job": _job(schedule.task),
        "name": name,
        "mode": str(raw["mode"]) if "mode" in raw else MODE_SHADOW,  # as parse_guard reads it
        "allowlisted": name in commands,
    }


async def get_guard_data(
    session: AsyncSession, settings: Settings, days: int, now: datetime | None = None
) -> dict[str, Any]:
    """The guards block for the last ``days`` days; ``schedules`` lists this agent's active guarded schedules."""
    agent_id = settings.agent_id
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    schedules = (
        await session.execute(
            select(Schedule)
            .where(Schedule.agent_id == agent_id, Schedule.active.is_(True), Schedule.metadata_.has_key(GUARD_KEY))
            .order_by(Schedule.created_at)
        )
    ).scalars().all()
    events = (
        await session.execute(
            select(Event.event_type, Event.data, Event.created_at).where(
                Event.agent_id == agent_id, Event.event_type.in_((RAN, UNRESOLVED, ACTED)), Event.created_at >= since
            )
        )
    ).all()
    # A schedule whose guard was removed or that was stopped keeps its history: its records name it.
    ids = {s.id.hex for s in schedules} | {
        h for e in events if isinstance(e.data, dict) and (h := _hex(e.data.get("schedule_id")))
    }
    runs = (
        await session.execute(
            select(Subtask.metadata_, Subtask.report_jsonb, Subtask.id, Subtask.completed_at).where(
                Subtask.agent_id == agent_id,
                Subtask.created_at >= since,
                or_(
                    Subtask.metadata_["schedule_id"].astext.in_(sorted(ids)),
                    Subtask.report_jsonb["guard_skipped"].astext == "true",
                ),
            )
        )
    ).all()

    acted: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for r in runs:
        report = r.report_jsonb if isinstance(r.report_jsonb, dict) else {}
        if report.get("guard_skipped") is True and isinstance(report.get(GUARD_KEY), dict):
            meta = r.metadata_ if isinstance(r.metadata_, dict) else {}
            records.append(
                {"at": as_utc(r.completed_at), "guard": report[GUARD_KEY], "subtask_id": r.id.hex,
                 "schedule_id": _hex(meta.get("schedule_id")), "ran": True}
            )  # fmt: skip
    for e in events:
        data = e.data if isinstance(e.data, dict) else {}
        guard = data.get(GUARD_KEY) if isinstance(data.get(GUARD_KEY), dict) else {}
        if e.event_type == ACTED:
            if (sid := data.get("subtask_id")) is not None:
                acted[sid] = data
            continue
        records.append(
            {"at": as_utc(e.created_at), "guard": guard, "subtask_id": data.get("subtask_id"),
             "schedule_id": _hex(data.get("schedule_id")), "ran": e.event_type == RAN}
        )  # fmt: skip

    fell_through = dict.fromkeys(REASONS, 0)
    counts = {"skipped": 0, "would_skip": 0}
    rows = []
    for rec in records:
        guard = dict(rec["guard"])
        guard.setdefault("mode", MODE_SKIP)  # every guard before phase 2 skipped
        kind, reason = _classify(guard, rec["ran"])
        if kind == "fell_through":
            fell_through[reason] += 1
        else:
            counts[kind] += 1
        a = acted.get(rec["subtask_id"]) if rec["subtask_id"] else None
        rows.append(
            {
                "at": rec["at"].isoformat(),
                "kind": kind,
                "schedule_id": rec["schedule_id"],
                "subtask_id": rec["subtask_id"],
                **{k: guard.get(k) for k in RECORD_KEYS},
                "reason": reason,
                "acted": a.get("acted") if a else None,
                "acted_by": a.get("acted_by") if a else None,
            }
        )
    rows.sort(key=lambda row: row["at"], reverse=True)

    misses = [
        a for a in acted.values()
        if a.get("acted") is True and isinstance(a.get(GUARD_KEY), dict) and a[GUARD_KEY].get("would_skip") is True
        and a[GUARD_KEY].get("mode") == MODE_SHADOW
    ]  # fmt: skip
    by_signal = {s: sum(1 for a in misses if s in (a.get("acted_by") or ())) for s in SIGNALS}
    return {
        "enabled": settings.schedule_guards_enabled,
        # The guard records other than skips are bus events: with the bus off none was persisted.
        "events_persisted": settings.event_bus_enabled,
        "window_days": days,
        "schedules": [_schedule_row(s, settings.schedule_guard_commands) for s in schedules],
        "runs": sum(1 for r in runs if _hex((r.metadata_ or {}).get("schedule_id")) in ids),
        "guard_runs": sum(1 for rec in records if rec["ran"]),
        "skipped": counts["skipped"],
        "would_skip": counts["would_skip"],
        "fell_through": fell_through,
        "shadow_misses": len(misses),
        "shadow_misses_by_signal": by_signal,
        "recent": rows[:RECENT_LIMIT],
    }
