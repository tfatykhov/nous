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
from nous.storage.models import DynamicCheckModel, Event, Schedule, Subtask

pytestmark = pytest.mark.postgres_only  # JSONB reads (metadata->>'schedule_id', report_jsonb->>'guard_skipped')

NOW = datetime.now(UTC)
NAME = "sweep"
REASONS = {"output", "stderr", "exit", "timeout", "error", "not_allowlisted", "invalid"}
COUNTS = (
    "runs", "guard_runs", "skipped", "would_skip", "would_skip_unrecorded", "would_skip_unobserved", "shadow_misses",
)  # fmt: skip


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


def acted(agent, sub, sch, signals, *, would_skip=True, age_hours=1, observed=True) -> Event:
    guard = {"name": NAME, "mode": "shadow", "would_skip": would_skip}
    return event(
        agent, "subtask_guard_acted", sub, sch, age_hours=age_hours, guard=guard, acted=bool(signals),
        acted_by=signals, observed=observed, run_status="completed", ledger_persisted=True,
    )  # fmt: skip


async def test_the_shape_with_nothing_guarded(db, agent):
    out = await data(db, agent)
    assert out == {
        "enabled": True,
        "bus_enabled": True,
        "window_days": 7,
        "schedules": [],
        "runs": 0,
        "guard_runs": 0,
        "skipped": 0,
        "would_skip": 0,
        "would_skip_unrecorded": 0,
        "would_skip_unobserved": 0,
        "fell_through": {r: 0 for r in REASONS},
        "shadow_misses": 0,
        "shadow_misses_by_signal": {s: 0 for s in ("report", "finding", "inbox", "push", "ledger")},
        "other": None,
        "checks": [],
        "recent": [],
    }
    assert (await data(db, agent, schedule_guards_enabled=False))["enabled"] is False
    assert (await data(db, agent, event_bus_enabled=False))["bus_enabled"] is False
    # the guard records are bus events: F026 persistence, the response's events_persisted, does not gate them
    assert (await data(db, agent, f026_persistence_enabled=False))["bus_enabled"] is True


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
    rows.append(
        event(agent, "subtask_guard_unresolved", ran[6], sch, guard={"name": "nope", "reason": "not_allowlisted"})
    )
    rows.append(event(agent, "subtask_guard_ran", ran[0], sch, age_hours=24 * 8, guard=record(reason="exit")))
    await add(db, sch, old, *skipped, *ran)
    await add(db, *rows)
    out = await data(db, agent)
    assert out["runs"] == 8  # the 8-day-old run is out of the window
    assert out["guard_runs"] == 7  # the skip and six command runs; the unresolved guard ran nothing
    assert (out["skipped"], out["would_skip"]) == (1, 1)
    assert out["fell_through"] == {r: int(r != "invalid") for r in REASONS}  # only a check's guard is invalid


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
    # Acting is a finding or a ledger write: report, inbox and push come with every completed run (PR #811 review).
    sch = schedule(agent, {"name": NAME})
    subs = [run(agent, sch) for _ in range(7)]
    await add(db, sch, *subs)
    await add(
        db,
        *(event(agent, "subtask_guard_ran", s, sch, guard=record()) for s in subs[:6]),
        event(agent, "subtask_guard_ran", subs[6], sch, guard=record(would_skip=False, reason="output")),
        acted(agent, subs[0], sch, ["report", "ledger"]),
        acted(agent, subs[1], sch, ["report", "finding", "inbox"]),
        acted(agent, subs[2], sch, ["report", "inbox"]),  # a "nothing to log" run, hardened path
        acted(agent, subs[3], sch, ["inbox", "push"]),  # the same, legacy path
        acted(agent, subs[4], sch, []),
        acted(agent, subs[6], sch, ["ledger"], would_skip=False),  # the guard said there was work: not a miss
    )
    out = await data(db, agent)
    assert (out["would_skip"], out["shadow_misses"]) == (6, 2)
    assert out["shadow_misses_by_signal"] == {"report": 2, "finding": 1, "inbox": 1, "push": 0, "ledger": 1}
    by_id = {r["subtask_id"]: r for r in out["recent"]}
    assert [by_id[s.id.hex]["acted"] for s in subs[:5]] == [True, True, False, False, False]
    assert by_id[subs[5].id.hex]["acted"] is None  # no acted record


async def test_acted_is_derived_from_acted_by_not_the_stored_flag(db, agent):
    # Records written before the narrowing carry acted=True for a report-only run.
    sch = schedule(agent, {"name": NAME})
    sub = run(agent, sch)
    old = acted(agent, sub, sch, ["report", "inbox"])
    old.data = {**old.data, "acted": True}
    await add(db, sch, sub)
    await add(db, event(agent, "subtask_guard_ran", sub, sch, guard=record()), old)
    out = await data(db, agent)
    assert out["shadow_misses"] == 0 and out["recent"][0]["acted"] is False


async def test_recent_holds_the_last_ten_records_newest_first_with_acted_joined(db, agent):
    sch = schedule(agent, {"name": NAME})
    subs = [run(agent, sch, age_hours=h + 1) for h in range(12)]
    skip = run(agent, sch, age_hours=0.5, report={"guard_skipped": True, "guard": record(mode="skip", skipped=True)})
    events = [
        event(agent, "subtask_guard_ran", s, sch, age_hours=i + 1, guard=record(stderr_head="<b>x</b>"))
        for i, s in enumerate(subs)
    ]
    await add(db, sch, skip, *subs)
    await add(db, *events, acted(agent, subs[0], sch, ["report", "ledger"]))
    recent = (await data(db, agent))["recent"]
    assert len(recent) == dash.RECENT_LIMIT == 10
    assert [r["at"] for r in recent] == sorted((r["at"] for r in recent), reverse=True)
    assert (recent[0]["kind"], recent[0]["subtask_id"]) == ("skipped", skip.id.hex)
    assert (recent[1]["kind"], recent[1]["subtask_id"]) == ("would_skip", subs[0].id.hex)
    assert (recent[1]["acted"], recent[1]["acted_by"]) == (True, ["report", "ledger"])
    assert recent[2]["acted"] is None  # no acted record (yet)
    assert recent[1]["stderr_head"] == "<b>x</b>"  # raw text; the view escapes it
    assert set(recent[1]) == {
        "at", "kind", "schedule_id", "subtask_id", "check_id", "name", "mode", "reason", "exit_code", "timed_out",
        "duration_ms", "stdout_bytes", "stderr_bytes", "stderr_head", "command", "acted", "acted_by", "observed",
    }  # fmt: skip


async def test_each_guarded_schedule_has_its_own_counts_and_last_runs(db, agent):
    hourly = schedule(agent, {"name": NAME})
    daily = schedule(agent, {"name": NAME, "mode": "skip"}, task="Check the grid bot.")
    ticks = [run(agent, hourly, age_hours=h + 0.5) for h in range(12)]  # newer than every daily run
    days_ = [run(agent, daily, age_hours=24 * d + 13) for d in range(3)]
    days_[0].report_jsonb = {"guard_skipped": True, "guard": record(mode="skip", skipped=True)}
    await add(db, hourly, daily, *ticks, *days_)
    await add(
        db,
        *(event(agent, "subtask_guard_ran", t, hourly, age_hours=h + 0.5, guard=record()) for h, t in enumerate(ticks)),
        acted(agent, ticks[0], hourly, ["report", "ledger"]),
        acted(agent, ticks[1], hourly, ["report", "inbox"]),
        *(acted(agent, t, hourly, ["report"]) for t in ticks[2:11]),  # ticks[11] has no acted record
        event(agent, "subtask_guard_ran", days_[1], daily, age_hours=37, guard=record(mode="skip", reason="exit")),
        event(agent, "subtask_guard_ran", days_[2], daily, age_hours=61, guard=record(mode="skip", reason="stderr")),
        acted(agent, days_[1], daily, ["finding"]),  # no would-skip record behind it: not a miss
    )
    out = await data(db, agent)
    rows = {r["schedule_id"]: r for r in out["schedules"]}
    h, d = rows[hourly.id.hex], rows[daily.id.hex]
    assert (h["runs"], h["guard_runs"], h["skipped"], h["would_skip"], h["would_skip_unrecorded"]) == (12, 12, 0, 12, 1)
    assert (h["shadow_misses"], h["shadow_misses_by_signal"]["ledger"]) == (1, 1)
    assert h["fell_through"] == {r: 0 for r in REASONS}
    assert (d["runs"], d["guard_runs"], d["skipped"], d["would_skip"], d["shadow_misses"]) == (3, 3, 1, 0, 0)
    assert (d["fell_through"]["exit"], d["fell_through"]["stderr"]) == (1, 1)
    # the hourly job fills the global last 10, but never the daily job's own list
    assert {r["schedule_id"] for r in out["recent"]} == {hourly.id.hex}
    assert [r["subtask_id"] for r in h["last_runs"]] == [t.id.hex for t in ticks[:10]]
    assert [r["subtask_id"] for r in d["last_runs"]] == [s.id.hex for s in days_]
    for row in (h, d, out):
        assert row["shadow_misses"] <= row["would_skip"]
    for key in COUNTS:
        assert out[key] == h[key] + d[key]
    assert out["other"] is None
    assert_totals_are_the_sum_of_the_rows(out)
    assert set(h) == {
        "schedule_id", "job", "name", "mode", "allowlisted", "runs", "guard_runs", "skipped", "would_skip",
        "would_skip_unrecorded", "would_skip_unobserved", "fell_through", "shadow_misses",
        "shadow_misses_by_signal", "last_runs",
    }  # fmt: skip


async def test_history_of_a_schedule_whose_guard_was_removed_still_counts(db, agent):
    sch = schedule(agent, None)  # its guard was removed after these runs
    sub = run(agent, sch)
    await add(db, sch, sub)
    await add(
        db, event(agent, "subtask_guard_ran", sub, sch, guard=record(mode="skip", would_skip=False, reason="exit"))
    )
    out = await data(db, agent)
    assert out["schedules"] == [] and (out["runs"], out["guard_runs"], out["fell_through"]["exit"]) == (1, 1, 1)
    # in no schedule row, so in the Other row: the rows add up to the totals (PR #811 review, P3-A)
    other = out["other"]
    assert (other["runs"], other["guard_runs"], other["fell_through"]["exit"]) == (1, 1, 1)
    assert [r["subtask_id"] for r in other["last_runs"]] == [sub.id.hex]


def assert_totals_are_the_sum_of_the_rows(out: dict) -> None:
    rows = [*out["schedules"], *([out["other"]] if out["other"] else []), *out["checks"]]
    for key in COUNTS:
        assert out[key] == sum(r[key] for r in rows), key
    for key in ("fell_through", "shadow_misses_by_signal"):
        for sub in out[key]:
            assert out[key][sub] == sum(r[key][sub] for r in rows), (key, sub)


async def test_the_totals_are_the_sum_of_the_rows_with_other(db, agent):
    shadow = schedule(agent, {"name": NAME})
    stopped = schedule(agent, {"name": NAME, "mode": "skip"}, active=False)
    removed = schedule(agent, None)  # its guard was removed after its runs
    unresolved = schedule(agent, {"name": "nope"})  # active and guarded: its own row, not Other
    subs = {k: run(agent, sch) for k, sch in (("shadow", shadow), ("removed", removed), ("unresolved", unresolved))}
    skipped = run(agent, stopped, report={"guard_skipped": True, "guard": record(mode="skip", skipped=True)})
    await add(db, shadow, stopped, removed, unresolved, *subs.values(), skipped)
    await add(
        db,
        event(agent, "subtask_guard_ran", subs["shadow"], shadow, guard=record()),
        acted(agent, subs["shadow"], shadow, ["ledger"]),
        event(agent, "subtask_guard_ran", subs["removed"], removed, guard=record()),
        acted(agent, subs["removed"], removed, ["finding"]),
        event(agent, "subtask_guard_unresolved", subs["unresolved"], unresolved, guard={"name": "nope"}),
        # an unresolved record naming no schedule
        Event(agent_id=agent, event_type="subtask_guard_unresolved", data={"guard": {"name": "x"}},
              created_at=NOW - timedelta(hours=2)),
    )  # fmt: skip
    out = await data(db, agent)
    assert {r["schedule_id"] for r in out["schedules"]} == {shadow.id.hex, unresolved.id.hex}
    other = out["other"]
    assert (other["runs"], other["guard_runs"], other["skipped"], other["would_skip"]) == (2, 2, 1, 1)
    assert (other["shadow_misses"], other["fell_through"]["not_allowlisted"]) == (1, 1)
    assert (out["shadow_misses"], out["fell_through"]["not_allowlisted"]) == (2, 2)
    assert len(other["last_runs"]) == 3
    assert_totals_are_the_sum_of_the_rows(out)


async def test_there_is_no_other_row_when_every_record_has_its_row(db, agent):
    sch = schedule(agent, {"name": NAME})
    sub = run(agent, sch)
    await add(db, sch, sub)
    await add(db, event(agent, "subtask_guard_ran", sub, sch, guard=record()))
    out = await data(db, agent)
    assert out["other"] is None
    assert_totals_are_the_sum_of_the_rows(out)


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


async def test_skips_of_a_stopped_schedule_still_count_as_its_runs(db, agent):
    sch = schedule(agent, {"name": NAME, "mode": "skip"}, active=False)  # skipped only, then stopped
    subs = [
        run(agent, sch, report={"guard_skipped": True, "guard": record(mode="skip", skipped=True)}) for _ in range(2)
    ]
    await add(db, sch, *subs)
    out = await data(db, agent)
    assert out["schedules"] == [] and (out["runs"], out["skipped"]) == (2, 2)
    assert (out["other"]["runs"], out["other"]["skipped"]) == (2, 2)


async def test_an_unresolved_guard_shows_in_recent_with_no_mode(db, agent):
    sch = schedule(agent, {"name": "nope"})
    sub = run(agent, sch)
    await add(db, sch, sub)
    await add(
        db, event(agent, "subtask_guard_unresolved", sub, sch, guard={"name": "nope", "reason": "not_allowlisted"})
    )
    (row,) = (await data(db, agent))["recent"]
    assert (row["kind"], row["reason"], row["mode"], row["name"], row["exit_code"]) == (
        "fell_through", "not_allowlisted", None, "nope", None,
    )  # fmt: skip


async def test_a_miss_counts_once_per_run_and_only_in_shadow_mode(db, agent):
    sch = schedule(agent, {"name": NAME})
    skipped = {"guard_skipped": True, "guard": record(mode="skip", skipped=True)}
    subs = [run(agent, sch), run(agent, sch, report=skipped)]
    skip_mode = acted(agent, subs[1], sch, ["ledger"])
    skip_mode.data = {**skip_mode.data, "guard": {"name": NAME, "mode": "skip", "would_skip": True}}
    await add(db, sch, *subs)
    await add(
        db, event(agent, "subtask_guard_ran", subs[0], sch, guard=record()),
        acted(agent, subs[0], sch, ["finding"]), acted(agent, subs[0], sch, ["finding"]), skip_mode,
    )  # fmt: skip
    assert (await data(db, agent))["shadow_misses"] == 1


async def test_an_acted_record_without_a_would_skip_ran_record_is_not_a_miss(db, agent):
    # The ran event was dropped by the best-effort bus: the acted record alone does not make a miss.
    sch = schedule(agent, {"name": NAME})
    sub = run(agent, sch)
    await add(db, sch, sub)
    await add(db, acted(agent, sub, sch, ["ledger"]))
    out = await data(db, agent)
    assert (out["would_skip"], out["shadow_misses"]) == (0, 0)


async def test_a_miss_needs_its_ran_record_in_the_window(db, agent):
    sch = schedule(agent, {"name": NAME})
    edge, inside = run(agent, sch, age_hours=24 * 7 + 0.1), run(agent, sch)
    await add(db, sch, edge, inside)
    await add(
        db,
        event(agent, "subtask_guard_ran", edge, sch, age_hours=24 * 7 + 0.1, guard=record()),  # just out
        acted(agent, edge, sch, ["ledger"], age_hours=24 * 7 - 0.1),  # just in: its run's turn ended later
        event(agent, "subtask_guard_ran", inside, sch, guard=record()),
        acted(agent, inside, sch, ["ledger"]),
    )
    out = await data(db, agent)
    assert (out["would_skip"], out["shadow_misses"]) == (1, 1)
    assert out["shadow_misses"] <= out["would_skip"]


async def test_a_skip_whose_write_raised_after_commit_counts_once_as_skipped(db, agent):
    # complete() committed the guard_skipped row, then raised: the worker also emitted ran(reason="error").
    sch = schedule(agent, {"name": NAME, "mode": "skip"})
    sub = run(agent, sch, report={"guard_skipped": True, "guard": record(mode="skip", skipped=True)})
    await add(db, sch, sub)
    # emitted after the commit, so newer than the row: the row still wins
    late = event(agent, "subtask_guard_ran", sub, sch, age_hours=0.9, guard=record(mode="skip", reason="error"))
    assert late.created_at > sub.completed_at
    await add(db, late)
    out = await data(db, agent)
    assert (out["guard_runs"], out["skipped"], out["fell_through"]["error"]) == (1, 1, 0)
    assert [(r["kind"], r["subtask_id"]) for r in out["recent"]] == [("skipped", sub.id.hex)]


async def test_a_run_reclaimed_after_a_shutdown_counts_once_by_its_newest_record(db, agent):
    # The row went back to pending and its guard ran again under the same subtask id.
    sch = schedule(agent, {"name": NAME})
    sub = run(agent, sch, age_hours=2)
    await add(db, sch, sub)
    await add(
        db,
        event(agent, "subtask_guard_ran", sub, sch, age_hours=2, guard=record()),
        event(agent, "subtask_guard_ran", sub, sch, age_hours=1, guard=record(would_skip=False, reason="output")),
    )
    out = await data(db, agent)
    assert (out["guard_runs"], out["would_skip"], out["fell_through"]["output"]) == (1, 0, 1)
    assert len(out["recent"]) == 1 and out["recent"][0]["reason"] == "output"
    await add(db, event(agent, "subtask_guard_ran", sub, sch, age_hours=0.5, guard=record()))
    assert (await data(db, agent))["would_skip"] == 1  # the newest wins again; the older would-skip is not added


async def test_would_skip_runs_with_no_outcome_record_are_counted_apart(db, agent):
    sch = schedule(agent, {"name": NAME})
    subs = [run(agent, sch) for _ in range(3)]
    await add(db, sch, *subs)
    await add(
        db, *(event(agent, "subtask_guard_ran", s, sch, guard=record()) for s in subs), acted(agent, subs[0], sch, [])
    )
    out = await data(db, agent)
    assert (out["would_skip"], out["would_skip_unrecorded"]) == (3, 2)


async def test_a_legacy_would_skip_run_is_not_observed_and_not_clean(db, agent):
    # The legacy path writes no structured report: a finding in its free text is no signal (PR #811, Codex P2).
    sch = schedule(agent, {"name": NAME})
    clean, legacy, legacy_wrote, older = (run(agent, sch) for _ in range(4))
    old = acted(agent, older, sch, ["inbox", "push"])
    old.data = {k: v for k, v in old.data.items() if k != "observed"}  # written before "observed" existed
    await add(db, sch, clean, legacy, legacy_wrote, older)
    await add(
        db,
        *(event(agent, "subtask_guard_ran", s, sch, guard=record()) for s in (clean, legacy, legacy_wrote, older)),
        acted(agent, clean, sch, ["report", "inbox"]),
        acted(agent, legacy, sch, ["inbox", "push"], observed=False),
        acted(agent, legacy_wrote, sch, ["inbox", "ledger"], observed=False),  # a ledger write is seen anyway
        old,
    )
    out = await data(db, agent)
    by_id = {r["subtask_id"]: (r["acted"], r["observed"]) for r in out["recent"]}
    assert by_id[legacy.id.hex] == (None, False)
    assert by_id[legacy_wrote.id.hex] == (True, False)
    assert by_id[clean.id.hex] == by_id[older.id.hex] == (False, True)
    clean_runs = sum(1 for acted_, _ in by_id.values() if acted_ is False)
    assert clean_runs == 2  # the legacy run is not clean
    (row,) = out["schedules"]
    for counts in (row, out):
        assert (counts["would_skip"], counts["would_skip_unobserved"], counts["would_skip_unrecorded"]) == (4, 1, 0)
        assert counts["shadow_misses"] == 1
        parts = clean_runs + counts["shadow_misses"] + counts["would_skip_unrecorded"] + counts["would_skip_unobserved"]
        assert parts == counts["would_skip"]


async def test_malformed_records_do_not_break_the_block(db, agent):
    sch = schedule(agent, {"name": NAME})
    sub = run(agent, sch)
    bad = acted(agent, sub, sch, ["report"])
    bad.data = {**bad.data, "acted_by": 5}
    await add(db, sch, sub)
    await add(
        db,
        bad,
        event(agent, "subtask_guard_acted", sub, sch, guard="x", acted=True),
        Event(agent_id=agent, event_type="subtask_guard_ran", data={"subtask_id": ["x"], "guard": 3}),
        Event(agent_id=agent, event_type="subtask_guard_unresolved", data={"schedule_id": 7}),
    )
    out = await data(db, agent)
    assert out["guard_runs"] == 1 and out["fell_through"]["not_allowlisted"] == 1


# --- heartbeat dynamic checks (F107 phase 3) ---------------------------------------------------------------


def check(agent, guard, *, name="gridbot-paper-tick", enabled=True) -> DynamicCheckModel:
    return DynamicCheckModel(
        id=uuid.uuid4(),
        agent_id=agent,
        name=name,
        description="tick",
        prompt="Diagnose the tick.",
        timeout_seconds=900,
        enabled=enabled,
        metadata_={} if guard is None else {"guard": guard},
    )


def check_event(agent, event_type, chk, run_id, *, age_hours=1, **data) -> Event:
    return Event(
        agent_id=agent,
        event_type=event_type,
        session_id=run_id,
        # check_id as DynamicCheck holds it: str(uuid), with dashes
        data={"check_id": str(chk.id), "check_name": chk.name, "run_id": run_id, **data},
        created_at=NOW - timedelta(hours=age_hours),
    )


def check_acted(agent, chk, run_id, signals, *, observed=True, age_hours=1) -> Event:
    return check_event(
        agent, "check_guard_acted", chk, run_id, age_hours=age_hours - 0.01,
        guard={"name": NAME, "mode": "shadow", "would_skip": True}, acted=bool(signals), acted_by=signals,
        observed=observed, run_status="completed", ledger_persisted=True,
    )  # fmt: skip


async def test_each_guarded_check_has_its_own_row_and_counts_in_the_totals(db, agent):
    sch = schedule(agent, {"name": NAME})
    sub = run(agent, sch)
    tick = check(agent, {"name": NAME, "mode": "shadow", "is_action": True})
    plain = check(agent, None, name="plain")  # no guard, no records: no row
    await add(db, sch, sub, tick, plain)
    runs = [f"dynamic-check-gridbot-paper-tick-{i}" for i in range(5)]
    await add(
        db,
        event(agent, "subtask_guard_ran", sub, sch, guard=record()),
        acted(agent, sub, sch, []),
        check_event(agent, "check_guard_ran", tick, runs[0], age_hours=5, guard=record()),
        check_acted(agent, tick, runs[0], [], age_hours=5),  # clean
        check_event(agent, "check_guard_ran", tick, runs[1], age_hours=4, guard=record()),
        check_acted(agent, tick, runs[1], ["finding"], age_hours=4),  # a shadow miss
        check_event(agent, "check_guard_ran", tick, runs[2], age_hours=3,
                    guard=record(would_skip=False, reason="output", stdout_bytes=40)),
        check_acted(agent, tick, runs[2], ["finding"], age_hours=3),  # fell through: not a miss
        check_event(agent, "check_guard_unresolved", tick, runs[3], age_hours=2,
                    guard={"name": NAME, "reason": "invalid", "detail": "guard timeout 900s ..."}),
        check_event(agent, "check_guard_ran", tick, runs[4], age_hours=8 * 24, guard=record()),  # out of window
    )  # fmt: skip

    out = await data(db, agent)

    [row] = out["checks"]
    assert (row["check_id"], row["check"], row["name"], row["mode"]) == (tick.id.hex, tick.name, NAME, "shadow")
    assert (row["allowlisted"], row["enabled"], row["timeout_seconds"]) == (True, True, 900)
    assert (row["runs"], row["guard_runs"], row["would_skip"], row["shadow_misses"]) == (4, 3, 2, 1)
    assert row["shadow_misses_by_signal"]["finding"] == 1
    assert (row["fell_through"]["output"], row["fell_through"]["invalid"]) == (1, 1)
    assert [r["check_id"] for r in row["last_runs"]] == [tick.id.hex] * 4
    assert [r["kind"] for r in row["last_runs"]] == ["fell_through", "fell_through", "would_skip", "would_skip"]
    assert row["last_runs"][0]["reason"] == "invalid"
    assert row["last_runs"][2]["acted"] is True
    assert (out["runs"], out["shadow_misses"], out["would_skip"]) == (5, 1, 3)
    assert {r["check_id"] for r in out["recent"]} == {None, tick.id.hex}
    assert_totals_are_the_sum_of_the_rows(out)


async def test_a_check_whose_guard_was_removed_keeps_its_row_while_it_has_records(db, agent):
    tick = check(agent, None)  # the guard is gone from its row
    await add(db, tick)
    await add(db, check_event(agent, "check_guard_ran", tick, "r1", guard=record(mode="skip", skipped=True)))

    out = await data(db, agent)

    [row] = out["checks"]
    assert (row["check"], row["name"], row["mode"], row["skipped"], row["runs"]) == (tick.name, None, None, 1, 1)
    assert out["skipped"] == 1


async def test_malformed_check_records_do_not_break_the_block(db, agent):
    tick = check(agent, {"name": NAME})
    await add(db, tick)
    await add(
        db,
        Event(agent_id=agent, event_type="check_guard_ran", data={"run_id": 5, "check_id": str(tick.id)}),
        Event(agent_id=agent, event_type="check_guard_ran", data={"run_id": "r", "guard": 3}),
        Event(agent_id=agent, event_type="check_guard_acted", data={"run_id": "r", "check_id": str(tick.id)}),
        check_event(agent, "check_guard_ran", tick, "r2", guard=3),
    )
    out = await data(db, agent)
    [row] = out["checks"]
    assert row["runs"] == 1 and sum(row["fell_through"].values()) == 1


async def test_a_check_whose_guard_is_null_is_listed_as_a_refused_guard(db, agent):
    """The runner refuses ``{"guard": null}`` as invalid, so the dashboard lists it and its invalid records."""
    tick = check(agent, {})
    tick.metadata_ = {"guard": None}
    await add(db, tick)
    await add(
        db,
        check_event(agent, "check_guard_unresolved", tick, "r1",
                    guard={"name": None, "reason": "invalid", "detail": "metadata.guard is not a JSON object"}),
    )  # fmt: skip

    out = await data(db, agent)

    [row] = out["checks"]
    assert (row["check_id"], row["name"], row["allowlisted"]) == (tick.id.hex, None, False)
    assert (row["runs"], row["guard_runs"], row["fell_through"]["invalid"]) == (1, 0, 1)
