"""The ``guards`` section of GET /dashboard/autonomy (F107 phase 2): what the scheduled-run guards did.

Read-only, and only from the database (``heart.schedules``, ``heart.subtasks``, ``nous_system.events``) and
Settings, so it shows what was persisted, never what one process happens to hold in memory. JSONB key reads,
so Postgres only, like ``job_fix_dashboard``. A guard run leaves one record: ``report_jsonb.guard`` on a row it
skipped, otherwise a ``subtask_guard_ran`` event; a guard naming no allowlisted command leaves a
``subtask_guard_unresolved`` event, and the turn of a shadow run a ``subtask_guard_acted`` event. Record texts
(command, stderr head, job) are returned as written; the view renders them as plain text.

Heartbeat dynamic checks carry guards too (F107 phase 3, ``nous/heartbeat/check_guard.py``): their records are
``check_guard_ran`` (skips included), ``check_guard_unresolved`` and ``check_guard_acted`` events keyed by the
check and its run (``run_id``). ``checks`` has one row per check with a guard now or a record in the window,
counted like a schedule's, and the totals and ``recent`` cover schedules and checks alike.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.dag.approval import as_utc
from nous.handlers.schedule_guard import MODE_SHADOW, MODE_SKIP, REASONS, SIGNALS, acted_verdict
from nous.storage.models import DynamicCheckModel, Event, Schedule, Subtask

GUARD_KEY = "guard"
RAN, UNRESOLVED, ACTED = "subtask_guard_ran", "subtask_guard_unresolved", "subtask_guard_acted"
# nous.heartbeat.check_guard's event types (not imported: this module reads only the database and Settings)
CHECK_RAN, CHECK_UNRESOLVED, CHECK_ACTED = "check_guard_ran", "check_guard_unresolved", "check_guard_acted"
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
    if not ran:  # a check's guard can also be refused as invalid (F107 phase 3)
        return "fell_through", "invalid" if guard.get("reason") == "invalid" else "not_allowlisted"
    if guard.get("skipped") is True:
        return "skipped", None
    reason = _reason(guard)
    if reason is None and guard.get("mode") == MODE_SHADOW:
        return "would_skip", None
    return "fell_through", reason or "error"


def _guard_fields(raw: object, commands: dict[str, str]) -> dict[str, Any]:
    """The guard name, mode and allowlisting of a row's ``metadata.guard``."""
    raw = raw if isinstance(raw, dict) else {}
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    return {
        "name": name,
        "mode": str(raw["mode"]) if "mode" in raw else MODE_SHADOW,  # as parse_guard reads it
        "allowlisted": name in commands,
    }


def _schedule_row(schedule: Schedule, commands: dict[str, str]) -> dict[str, Any]:
    return {
        "schedule_id": schedule.id.hex,
        "job": _job(schedule.task),
        **_guard_fields((schedule.metadata_ or {}).get(GUARD_KEY), commands),
    }


def _one_per_run(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per run (subtask id): the row's ``report_jsonb.guard`` over an event, else the newest.

    A run can leave two: a skip whose ``complete()`` committed and then raised leaves its skipped row and a
    ``ran`` event with reason ``error``, and a row reclaimed after a shutdown runs its guard again under the
    same id. Records with no subtask id are kept as they are.
    """
    best: dict[str, dict[str, Any]] = {}
    loose: list[dict[str, Any]] = []
    for rec in records:
        sid = rec["subtask_id"]
        if sid is None:
            loose.append(rec)
        elif sid not in best or (rec["row"], rec["at"]) >= (best[sid]["row"], best[sid]["at"]):
            best[sid] = rec
    return [*best.values(), *loose]


def _row(rec: dict[str, Any], outcome: dict[str, Any] | None) -> dict[str, Any]:
    """One record as the view shows it, with what the model did after it (``outcome``, the acted record)."""
    guard = dict(rec["guard"])
    if rec["ran"]:
        guard.setdefault("mode", MODE_SKIP)  # every guard before phase 2 skipped
    kind, reason = _classify(guard, rec["ran"])
    acted_by = _acted_by(outcome)
    # a record from before "observed" existed reads as observed, as it did when it was written
    observed = None if acted_by is None else outcome.get("observed") is not False
    return {
        "at": rec["at"].isoformat(),
        "kind": kind,
        "schedule_id": rec["schedule_id"],
        "subtask_id": rec["subtask_id"],
        "check_id": rec.get("check_id"),
        **{k: guard.get(k) for k in RECORD_KEYS},
        "reason": reason,
        # derived from acted_by, never the stored flag: one definition, also for older records
        "acted": acted_verdict(acted_by, observed=observed) if acted_by is not None else None,
        "acted_by": acted_by,
        # False: the run left no structured report (e.g. the legacy path), so "did nothing" could not be seen
        "observed": observed,
    }


def _tally(pairs: list[tuple[dict[str, Any], dict[str, Any]]], runs: int) -> dict[str, Any]:
    """The counts over ``(record, row)`` pairs; ``runs`` is the number of runs they belong to."""
    rows = [row for _, row in pairs]
    fell_through = dict.fromkeys(REASONS, 0)
    for row in rows:
        if row["kind"] == "fell_through":
            fell_through[row["reason"]] += 1
    would_skip = [row for row in rows if row["kind"] == "would_skip"]
    # Misses are counted over the would-skip records in the window, so shadow_misses <= would_skip: an acted
    # record whose ran record the bus dropped, or that fell out of the window, is not a miss.
    misses = [row["acted_by"] for row in would_skip if row["acted"] is True]
    return {
        "runs": runs,
        "guard_runs": sum(1 for rec, _ in pairs if rec["ran"]),
        "skipped": sum(1 for row in rows if row["kind"] == "skipped"),
        "would_skip": len(would_skip),
        # would-skip runs with no record of what the model did (its turn is still going, or the record was lost)
        "would_skip_unrecorded": sum(1 for row in would_skip if row["acted"] is None and row["observed"] is None),
        # would-skip runs with a record that could not see a finding (no structured report) and no ledger write
        "would_skip_unobserved": sum(1 for row in would_skip if row["acted"] is None and row["observed"] is False),
        "fell_through": fell_through,
        "shadow_misses": len(misses),
        "shadow_misses_by_signal": {s: sum(1 for kinds in misses if s in kinds) for s in SIGNALS},
    }


async def _check_rows(
    session: AsyncSession, agent_id: str, since: datetime, commands: dict[str, str]
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """One row per guarded heartbeat check, and the ``(record, row)`` pairs of all check records, newest first.

    A check run leaves one ran or unresolved record (keyed by ``run_id``); its acted record joins it.
    """
    checks = (
        (
            await session.execute(
                select(DynamicCheckModel)
                .where(DynamicCheckModel.agent_id == agent_id, DynamicCheckModel.metadata_.has_key(GUARD_KEY))
                .order_by(DynamicCheckModel.created_at)
            )
        )
        .scalars()
        .all()
    )
    events = (
        await session.execute(
            select(Event.event_type, Event.data, Event.created_at)
            .where(
                Event.agent_id == agent_id,
                Event.event_type.in_((CHECK_RAN, CHECK_UNRESOLVED, CHECK_ACTED)),
                Event.created_at >= since,
            )
            .order_by(Event.created_at)  # oldest first: of two records of one run, the newest wins
        )
    ).all()
    outcomes: dict[str, dict[str, Any]] = {}
    by_run: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for e in events:
        data = _dict(e.data)
        run_id = data.get("run_id") if isinstance(data.get("run_id"), str) else None
        check_id = _hex(data.get("check_id"))
        if run_id is None or check_id is None:
            continue
        if e.event_type == CHECK_ACTED:
            outcomes[run_id] = data
            continue
        if isinstance(data.get("check_name"), str):
            names[check_id] = data["check_name"]
        by_run[run_id] = {
            "at": as_utc(e.created_at), "guard": _dict(data.get(GUARD_KEY)), "subtask_id": None, "schedule_id": None,
            "check_id": check_id, "run_id": run_id, "ran": e.event_type == CHECK_RAN, "row": False,
        }  # fmt: skip
    pairs = sorted(
        ((rec, _row(rec, outcomes.get(rec["run_id"]))) for rec in by_run.values()),
        key=lambda pair: pair[1]["at"],
        reverse=True,
    )
    by_check: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for pair in pairs:
        by_check.setdefault(pair[0]["check_id"], []).append(pair)
    current = {c.id.hex: c for c in checks}
    rows = []
    # Checks with a guard now, then checks whose guard was removed (or whose row is gone) but left records.
    for check_id in [*current, *(h for h in by_check if h not in current)]:
        check = current.get(check_id)
        mine = by_check.get(check_id, [])
        rows.append(
            {
                "check_id": check_id,
                "check": check.name if check is not None else names.get(check_id),
                "enabled": check.enabled if check is not None else None,
                "timeout_seconds": check.timeout_seconds if check is not None else None,
                **(
                    _guard_fields((check.metadata_ or {}).get(GUARD_KEY), commands)
                    if check is not None
                    else {"name": None, "mode": None, "allowlisted": None}
                ),
                # every run of a check with a guard leaves one record, so its runs are its records
                **_tally(mine, len(mine)),
                "last_runs": [row for _, row in mine[:RECENT_LIMIT]],
            }
        )
    return rows, pairs


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

    pairs = sorted(
        ((rec, _row(rec, outcomes.get(rec["subtask_id"] or ""))) for rec in records),
        key=lambda pair: pair[1]["at"],
        reverse=True,
    )
    run_counts = Counter(_hex(_dict(r.metadata_).get("schedule_id")) for r in runs)
    by_schedule: dict[str | None, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for pair in pairs:  # newest first, like pairs
        by_schedule.setdefault(pair[0]["schedule_id"], []).append(pair)
    # What no schedule row holds: records of stopped or de-guarded schedules, and ones naming no schedule.
    active = {s.id.hex for s in schedules}
    other_pairs = [pair for pair in pairs if pair[0]["schedule_id"] not in active]
    other_runs = sum(run_counts[h] for h in ids - active)
    check_rows, check_pairs = await _check_rows(session, agent_id, since, settings.schedule_guard_commands)
    every_pair = sorted([*pairs, *check_pairs], key=lambda pair: pair[1]["at"], reverse=True)

    return {
        "enabled": settings.schedule_guards_enabled,
        # The guard records other than skips are bus events: with the bus off none was persisted. Not named
        # events_persisted, which beside it in the response is NOUS_F026_PERSISTENCE_ENABLED, a different switch.
        "bus_enabled": settings.event_bus_enabled,
        "window_days": days,
        # Each guarded schedule's own counts and last runs, so a second guard's records never mix with these.
        "schedules": [
            {
                **_schedule_row(s, settings.schedule_guard_commands),
                **_tally(by_schedule.get(s.id.hex, []), run_counts[s.id.hex]),
                "last_runs": [row for _, row in by_schedule.get(s.id.hex, [])[:RECENT_LIMIT]],
            }
            for s in schedules
        ],
        # The remainder of every count, so the rows add up to the totals; null when it is all zero.
        "other": (
            {**_tally(other_pairs, other_runs), "last_runs": [row for _, row in other_pairs[:RECENT_LIMIT]]}
            if other_pairs or other_runs
            else None
        ),
        # Each guarded heartbeat check's own counts and last runs (F107 phase 3).
        "checks": check_rows,
        # The totals, over every schedule with a guard record or a guard now and every guarded check.
        **_tally(every_pair, sum(run_counts[h] for h in ids) + len(check_pairs)),
        "recent": [row for _, row in every_pair[:RECENT_LIMIT]],
    }
