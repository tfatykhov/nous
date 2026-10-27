"""F107 phase 2: the ``guards`` block of GET /dashboard/autonomy (nous/api/guard_dashboard.py).

Built only from heart.schedules, heart.subtasks, nous_system.events and Settings, never from an in-memory ring.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from nous.api import guard_dashboard as dash
from nous.config import Settings
from nous.storage.models import Event, Schedule, Subtask

pytestmark = pytest.mark.postgres_only  # JSONB reads (metadata->>'schedule_id', report_jsonb->>'guard_skipped')

NOW = datetime.now(UTC)
NAME = "sweep"
REASONS = {"output", "stderr", "exit", "timeout", "error", "not_allowlisted"}


@pytest_asyncio.fixture
async def agent() -> str:
    return f"test-guards-{uuid.uuid4().hex[:8]}"


def settings(agent, **kw) -> Settings:
    return Settings(_env_file=None, agent_id=agent, schedule_guard_commands={NAME: "bash /x.sh"}, **kw)


async def add(db, *rows) -> None:
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()


async def data(db, agent, days=7, **kw):
    async with db.session() as s:
        return await dash.get_guard_data(s, settings(agent, **kw), days=days, now=NOW)


def schedule(agent, guard, *, task="Run the sweep.\nThen report.", active=True) -> Schedule:
    return Schedule(
        id=uuid.uuid4(),
        agent_id=agent,
        task=task,
        schedule_type="recurring",
        interval_seconds=3600,
        active=active,
        metadata_={} if guard is None else {"guard": guard},
    )


def run(agent, sch, *, age_hours=1, report=None) -> Subtask:
    return Subtask(
        id=uuid.uuid4(),
        agent_id=agent,
        task=sch.task,
        status="completed",
        metadata_={"schedule_id": sch.id.hex},
        report_jsonb=report,
        created_at=NOW - timedelta(hours=age_hours),
        completed_at=NOW - timedelta(hours=age_hours) + timedelta(minutes=1),
    )


def record(**kw) -> dict:
    base = {
        "name": NAME,
        "command": "bash /x.sh",
        "skip_when": "exit0_quiet",
        "timeout_seconds": 60,
        "exit_code": 0,
        "timed_out": False,
        "error": None,
        "duration_ms": 12,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stderr_head": "",
        "skipped": False,
        "mode": "shadow",
        "would_skip": True,
        "reason": None,
    }
    return {**base, **kw}


def event(agent, event_type, sub, sch, *, age_hours=1, **data) -> Event:
    return Event(
        agent_id=agent,
        event_type=event_type,
        session_id=f"subtask-{sub.id.hex[:8]}" if sub is not None else None,
        data={"subtask_id": sub.id.hex if sub is not None else None, "schedule_id": sch.id.hex, **data},
        created_at=NOW - timedelta(hours=age_hours) + timedelta(seconds=30),
    )


def acted(agent, sub, sch, signals, *, would_skip=True, age_hours=1) -> Event:
    guard = {"name": NAME, "mode": "shadow", "would_skip": would_skip}
    return event(
        agent, "subtask_guard_acted", sub, sch, age_hours=age_hours, guard=guard, acted=bool(signals),
        acted_by=signals, run_status="completed", ledger_persisted=True,
    )  # fmt: skip


async def test_the_shape_with_nothing_guarded(db, agent):
    out = await data(db, agent)
    assert out == {
        "enabled": True,
        "events_persisted": True,
        "window_days": 7,
        "schedules": [],
        "runs": 0,
        "guard_runs": 0,
        "skipped": 0,
        "would_skip": 0,
        "fell_through": {r: 0 for r in REASONS},
        "shadow_misses": 0,
        "shadow_misses_by_signal": {s: 0 for s in ("report", "finding", "inbox", "push", "ledger")},
        "recent": [],
    }
    assert (await data(db, agent, schedule_guards_enabled=False))["enabled"] is False
    assert (await data(db, agent, event_bus_enabled=False))["events_persisted"] is False


async def test_one_row_per_active_guarded_schedule_with_mode_name_and_a_clipped_job(db, agent):
    shadow = schedule(agent, {"name": NAME}, task="x" * 300 + "\nsecond line")
    skip = schedule(agent, {"name": "gone", "mode": "skip"})
    odd = schedule(agent, {"name": NAME, "mode": "dry_run"})
    await add(
        db, shadow, skip, odd, schedule(agent, None), schedule(agent, {"name": NAME}, active=False),
        schedule(f"{agent}-other", {"name": NAME}),
    )  # fmt: skip
    rows = {r["schedule_id"]: r for r in (await data(db, agent))["schedules"]}
    assert set(rows) == {shadow.id.hex, skip.id.hex, odd.id.hex}
    assert rows[shadow.id.hex]["mode"] == "shadow" and rows[shadow.id.hex]["name"] == NAME
    assert rows[shadow.id.hex]["allowlisted"] is True
    assert "\n" not in rows[shadow.id.hex]["job"] and len(rows[shadow.id.hex]["job"]) <= dash.JOB_CHARS + 1
    assert (rows[skip.id.hex]["mode"], rows[skip.id.hex]["allowlisted"]) == ("skip", False)
    assert rows[odd.id.hex]["mode"] == "dry_run"
    assert rows[skip.id.hex]["job"] == "Run the sweep."


async def test_window_counts_and_fell_through_by_reason(db, agent):
    sch = schedule(agent, {"name": NAME, "mode": "skip"})
    old = run(agent, sch, age_hours=24 * 8, report={"guard_skipped": True, "guard": record(skipped=True)})
    skipped = [run(agent, sch, report={"guard_skipped": True, "guard": record(mode="skip", skipped=True)})]
    ran = [run(agent, sch) for _ in range(7)]
    reasons = ["output", "stderr", "exit", "timeout", "error"]
    rows = [
        event(agent, "subtask_guard_ran", ran[i], sch, guard=record(mode="skip", would_skip=False, reason=r))
        for i, r in enumerate(reasons)
    ]
    rows.append(event(agent, "subtask_guard_ran", ran[5], sch, guard=record()))  # shadow, would skip
    rows.append(event(agent, "subtask_guard_unresolved", ran[6], sch, guard={"name": "nope", "reason": "not_allowlisted"}))
    rows.append(event(agent, "subtask_guard_ran", ran[0], sch, age_hours=24 * 8, guard=record(reason="exit")))
    await add(db, sch, old, *skipped, *ran)
    await add(db, *rows)
    out = await data(db, agent)
    assert out["runs"] == 8  # the 8-day-old run is out of the window
    assert out["guard_runs"] == 7  # the skip and six command runs; the unresolved guard ran nothing
    assert (out["skipped"], out["would_skip"]) == (1, 1)
    assert out["fell_through"] == {r: 1 for r in REASONS}


async def test_a_record_from_before_phase_2_counts_as_skip_mode_with_a_derived_reason(db, agent):
    sch = schedule(agent, {"name": NAME, "mode": "skip"})
    sub = run(agent, sch)
    legacy = {k: v for k, v in record(stdout_bytes=12).items() if k not in ("mode", "would_skip", "reason")}
    await add(db, sch, sub)
    await add(db, event(agent, "subtask_guard_ran", sub, sch, guard=legacy))
    out = await data(db, agent)
    assert out["fell_through"]["output"] == 1 and out["would_skip"] == 0
    assert out["recent"][0]["mode"] == "skip"


async def test_shadow_misses_are_would_skip_runs_where_the_model_acted(db, agent):
    sch = schedule(agent, {"name": NAME})
    subs = [run(agent, sch) for _ in range(4)]
    await add(db, sch, *subs)
    await add(
        db,
        acted(agent, subs[0], sch, ["report", "ledger"]),
        acted(agent, subs[1], sch, ["inbox"]),
        acted(agent, subs[2], sch, []),
        acted(agent, subs[3], sch, ["ledger"], would_skip=False),  # the guard said there was work: not a miss
        acted(agent, subs[0], sch, ["push"], age_hours=24 * 8),
    )
    out = await data(db, agent)
    assert out["shadow_misses"] == 2
    assert out["shadow_misses_by_signal"] == {"report": 1, "finding": 0, "inbox": 1, "push": 0, "ledger": 1}


async def test_recent_holds_the_last_ten_records_newest_first_with_acted_joined(db, agent):
    sch = schedule(agent, {"name": NAME})
    subs = [run(agent, sch, age_hours=h + 1) for h in range(12)]
    skip = run(agent, sch, age_hours=0.5, report={"guard_skipped": True, "guard": record(mode="skip", skipped=True)})
    events = [
        event(agent, "subtask_guard_ran", s, sch, age_hours=i + 1, guard=record(stderr_head="<b>x</b>"))
        for i, s in enumerate(subs)
    ]
    await add(db, sch, skip, *subs)
    await add(db, *events, acted(agent, subs[0], sch, ["report"]))
    recent = (await data(db, agent))["recent"]
    assert len(recent) == dash.RECENT_LIMIT == 10
    assert [r["at"] for r in recent] == sorted((r["at"] for r in recent), reverse=True)
    assert (recent[0]["kind"], recent[0]["subtask_id"]) == ("skipped", skip.id.hex)
    assert (recent[1]["kind"], recent[1]["subtask_id"]) == ("would_skip", subs[0].id.hex)
    assert (recent[1]["acted"], recent[1]["acted_by"]) == (True, ["report"])
    assert recent[2]["acted"] is None  # no acted record (yet)
    assert recent[1]["stderr_head"] == "<b>x</b>"  # raw text; the view escapes it
    assert set(recent[1]) == {
        "at", "kind", "schedule_id", "subtask_id", "name", "mode", "reason", "exit_code", "timed_out",
        "duration_ms", "stdout_bytes", "stderr_bytes", "stderr_head", "command", "acted", "acted_by",
    }  # fmt: skip


async def test_history_of_a_schedule_whose_guard_was_removed_still_counts(db, agent):
    sch = schedule(agent, None)  # its guard was removed after these runs
    sub = run(agent, sch)
    await add(db, sch, sub)
    await add(db, event(agent, "subtask_guard_ran", sub, sch, guard=record(mode="skip", would_skip=False, reason="exit")))
    out = await data(db, agent)
    assert out["schedules"] == [] and (out["runs"], out["guard_runs"], out["fell_through"]["exit"]) == (1, 1, 1)


async def test_another_agents_records_are_not_counted(db, agent):
    sch = schedule(agent, {"name": NAME})
    other = f"{agent}-other"
    sub = run(other, sch)
    await add(db, sch, sub)
    await add(db, event(other, "subtask_guard_ran", sub, sch, guard=record()), acted(other, sub, sch, ["report"]))
    out = await data(db, agent)
    assert (out["runs"], out["guard_runs"], out["shadow_misses"], out["recent"]) == (0, 0, 0, [])


def test_the_block_reads_only_the_database_and_settings():
    # No runner, context logger, bus or worker: nothing in memory (the ring) can reach the block.
    params = list(inspect.signature(dash.get_guard_data).parameters)
    assert params == ["session", "settings", "days", "now"]
    source = inspect.getsource(dash)
    for name in ("context_log", "ContextLogger", "_entries", "runner", "_ledgers", "EventBus", "SubtaskWorkerPool"):
        assert name not in source
