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
from nous.handlers.schedule_guard import MODE_SHADOW, MODE_SKIP, REASONS, SIGNALS, acted
from nous.storage.models import Event, Schedule, Subtask

GUARD_KEY = "guard"
RAN, UNRESOLVED, ACTED = "subtask_guard_ran", "subtask_guard_unresolved", "subtask_guard_acted"
RECENT_LIMIT = 10
JOB_CHARS = 120
RECORD_KEYS = (
    "name", "mode", "reason", "exit_code", "timed_out", "duration_ms", "stdout_bytes", "stderr_bytes",
    "stderr_head", "command",
)  # fmt: skip


def _hex(value: object) -> str | None:
    return value.replace("-", "").lower() if isinstance(value, str) and value else None


def _dict(value: object) -> dict[str, Any]:
    """``value`` if it is a dict, else empty: these JSONB fields are written by code the agent can bypass."""
    return value if isinstance(value, dict) else {}


def _acted_by(data: dict[str, Any] | None) -> list[str] | None:
    """The kinds of action of an acted record, or None without one (or with an unreadable one)."""
    kinds = data.get("acted_by") if data else None
    return [k for k in kinds if isinstance(k, str)] if isinstance(kinds, list) else None


def _job(task: str) -> str:
    """The first non-empty line of the job's text, clipped."""
    line = next((ln.strip() for ln in (task or "").splitlines() if ln.strip()), "")
    return line[:JOB_CHARS] + "…" if len(line) > JOB_CHARS else line


def _reason(guard: dict[str, Any]) -> str | None:
    """A record's fall-through reason. A record from before phase 2 has none: derived from its fields."""
    if "reason" in guard:
        reason = guard["reason"]
        if reason in REASONS:
            return reason
        return "error" if reason else None
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


def _one_per_run(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per run (subtask id): the row's ``report_jsonb.guard`` over an event, else the newest.

    A run can leave two: a skip whose ``complete()`` committed and then raised leaves its skipped row and a
    ``ran`` event with reason ``error``, and a row reclaimed after a shutdown runs its guard again under the
    same id. Records with no subtask id are kept as they are.
    """
    best: dict[str, dict[str, Any]] = {}
    loose = []
    for rec in records:
        sid = rec["subtask_id"]
        if sid is None:
            loose.append(rec)
        elif sid not in best or (rec["row"], rec["at"]) >= (best[sid]["row"], best[sid]["at"]):
            best[sid] = rec
    return [*best.values(), *loose]


async def get_guard_data(
    session: AsyncSession, settings: Settings, days: int, now: datetime | None = None
) -> dict[str, Any]:
    """The guards block for the last ``days`` days; ``schedules`` lists this agent's active guarded schedules."""
    agent_id = settings.agent_id
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    schedules = (
        (
            await session.execute(
                select(Schedule)
                .where(Schedule.agent_id == agent_id, Schedule.active.is_(True), Schedule.metadata_.has_key(GUARD_KEY))
                .order_by(Schedule.created_at)
            )
        )
        .scalars()
        .all()
    )
    events = (
        await session.execute(
            select(Event.event_type, Event.data, Event.created_at)
            .where(
                Event.agent_id == agent_id, Event.event_type.in_((RAN, UNRESOLVED, ACTED)), Event.created_at >= since
            )
            .order_by(Event.created_at)  # oldest first: of two acted records of one run, the newest wins
        )
    ).all()
    # A schedule whose guard was removed or that was stopped keeps its history: its records name it
    # (a skipped run is its own record, so its schedule joins below).
    ids = {s.id.hex for s in schedules} | {h for e in events if (h := _hex(_dict(e.data).get("schedule_id")))}
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

    outcomes: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for r in runs:
        report = _dict(r.report_jsonb)
        if report.get("guard_skipped") is True and isinstance(report.get(GUARD_KEY), dict) and r.completed_at:
            schedule_id = _hex(_dict(r.metadata_).get("schedule_id"))
            if schedule_id:
                ids.add(schedule_id)
            records.append(
                {"at": as_utc(r.completed_at), "guard": report[GUARD_KEY], "subtask_id": r.id.hex,
                 "schedule_id": schedule_id, "ran": True, "row": True}
            )  # fmt: skip
    for e in events:
        data = _dict(e.data)
        sid = data.get("subtask_id") if isinstance(data.get("subtask_id"), str) else None
        if e.event_type == ACTED:
            if sid is not None:
                outcomes[sid] = data
            continue
        records.append(
            {"at": as_utc(e.created_at), "guard": _dict(data.get(GUARD_KEY)), "subtask_id": sid,
             "schedule_id": _hex(data.get("schedule_id")), "ran": e.event_type == RAN, "row": False}
        )  # fmt: skip
    records = _one_per_run(records)

    fell_through = dict.fromkeys(REASONS, 0)
    counts = {"skipped": 0, "would_skip": 0, "would_skip_unrecorded": 0}
    # The acted records of would-skip runs in the window, so shadow_misses <= would_skip: an acted record
    # whose ran record was dropped by the bus or fell out of the window is not a miss.
    misses: list[list[str]] = []
    rows = []
    for rec in records:
        guard = dict(rec["guard"])
        if rec["ran"]:
            guard.setdefault("mode", MODE_SKIP)  # every guard before phase 2 skipped
        kind, reason = _classify(guard, rec["ran"])
        a = outcomes.get(rec["subtask_id"]) if rec["subtask_id"] else None
        acted_by = _acted_by(a)
        if kind == "fell_through":
            fell_through[reason] += 1
        else:
            counts[kind] += 1
            if kind == "would_skip" and a is None:
                counts["would_skip_unrecorded"] += 1
            elif kind == "would_skip" and acted_by is not None and acted(acted_by):
                misses.append(acted_by)
        rows.append(
            {
                "at": rec["at"].isoformat(),
                "kind": kind,
                "schedule_id": rec["schedule_id"],
                "subtask_id": rec["subtask_id"],
                **{k: guard.get(k) for k in RECORD_KEYS},
                "reason": reason,
                # derived from acted_by, never the stored flag: one definition, also for older records
                "acted": acted(acted_by) if acted_by is not None else None,
                "acted_by": acted_by,
            }
        )
    rows.sort(key=lambda row: row["at"], reverse=True)

    by_signal = {s: sum(1 for kinds in misses if s in kinds) for s in SIGNALS}
    return {
        "enabled": settings.schedule_guards_enabled,
        # The guard records other than skips are bus events: with the bus off none was persisted.
        "events_persisted": settings.event_bus_enabled,
        "window_days": days,
        "schedules": [_schedule_row(s, settings.schedule_guard_commands) for s in schedules],
        "runs": sum(1 for r in runs if _hex(_dict(r.metadata_).get("schedule_id")) in ids),
        "guard_runs": sum(1 for rec in records if rec["ran"]),
        "skipped": counts["skipped"],
        "would_skip": counts["would_skip"],
        # would-skip runs with no record of what the model did (its turn is still going, or the record was lost)
        "would_skip_unrecorded": counts["would_skip_unrecorded"],
        "fell_through": fell_through,
        "shadow_misses": len(misses),
        "shadow_misses_by_signal": by_signal,
        "recent": rows[:RECENT_LIMIT],
    }
