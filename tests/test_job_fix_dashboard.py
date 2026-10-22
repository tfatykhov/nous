"""The check-and-fix section of GET /dashboard/autonomy, on real Postgres: one row per job_fix record."""

from __future__ import annotations

from datetime import timedelta

import pytest
from job_support import NOW, job_env, make_run, make_schedule  # noqa: F401

from nous.api.job_fix_dashboard import TEXT_CHARS, get_job_fix_data
from nous.heart.job_runs import FIX_KEY

pytestmark = [pytest.mark.postgres_only, pytest.mark.scoring_writer]


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


async def jobs(env) -> list[dict]:
    return (await get_job_fix_data(env.heart, enabled=True))["jobs"]


async def test_only_this_agents_records_are_listed_and_the_flag_is_passed_through(job_env):  # noqa: F811
    env, other = await job_env(), await job_env()
    await make_schedule(env, task="No record")
    await make_schedule(other, metadata={FIX_KEY: {"state": "running", "started_at": ago(1)}})
    assert await get_job_fix_data(env.heart, enabled=False) == {"enabled": False, "jobs": []}
    assert len(await jobs(other)) == 1


async def test_a_running_check_shows_its_trigger_run_and_baseline(job_env):  # noqa: F811
    env = await job_env()
    record = {
        "state": "running", "trigger": "fails_often", "fix_run": "1f" * 16, "started_at": ago(2),
        "baseline": {"bad": 4, "scored": 5, "window_end": ago(2)},
    }
    sched = await make_schedule(env, task="Run the grid tick", metadata={FIX_KEY: record})
    [row] = await jobs(env)
    assert row["schedule_id"] == sched.id.hex and row["job"] == "Run the grid tick" and row["active"] is True
    assert (row["state"], row["trigger"], row["verdict"], row["stop_reason"]) == ("running", "fails_often", None, None)
    assert row["fix_run"] == "1f" * 16 and row["started_at"] == ago(2)
    assert row["baseline"] == {"bad": 4, "scored": 5}  # window_end is not shown
    assert row["since_change"] is None and row["reported"] is False
    assert (row["old_task"], row["task"], row["detail"]) == (None, None, {})


async def test_judging_counts_the_runs_the_judge_counts(job_env):  # noqa: F811
    env = await job_env()
    record = {
        "state": "judging", "trigger": "fails_often", "verdict": "changed_job", "started_at": ago(30),
        "applied_at": ago(20), "old_task": "Old text", "baseline": {"bad": 3, "scored": 5},
        "detail": {"verdict": "changed_job", "new_instructions": "New text", "why": "the path moved"},
    }
    sched = await make_schedule(env, task="Old text")
    await env.heart.schedules.set_metadata(sched.id, FIX_KEY, record, task="New text")
    await make_run(env, sched, outcome="failed", age_hours=25)  # before the change: not counted
    await make_run(env, sched, outcome="failed", age_hours=10)
    await make_run(env, sched, outcome="done_self", age_hours=5, settled=False)  # provisional: counted
    await make_run(env, sched, outcome="failed", age_hours=4, fix=True)  # a fix run is never a run of the job
    [row] = await jobs(env)
    assert row["since_change"] == {"scored": 2, "bad": 1, "needed": 5}
    assert row["job"] == "Old text" and (row["old_task"], row["task"]) == ("Old text", "New text")
    assert row["detail"] == {"new_instructions": "New text", "why": "the path moved"}
    assert row["applied_at"] == ago(20)


async def test_a_stop_shows_the_check_text_and_whether_the_owner_was_told(job_env):  # noqa: F811
    env = await job_env()
    reported = {
        "state": "stopped", "stop_reason": "needs_owner", "verdict": "needs_owner", "started_at": ago(9),
        "report_id": "ab" * 16, "old_task": "Same text",
        "detail": {"diagnosis": "the token expired", "owner_action": "renew it", "check": {"status": "completed"}},
    }
    await make_schedule(env, task="Same text", active=False, metadata={FIX_KEY: reported})
    await make_schedule(env, active=False, metadata={FIX_KEY: {"state": "stopped", "stop_reason": "inactive"}})
    rows = await jobs(env)
    told = next(r for r in rows if r["stop_reason"] == "needs_owner")
    assert told["reported"] is True and told["active"] is False
    assert told["detail"] == {
        "diagnosis": "the token expired", "owner_action": "renew it", "check": {"status": "completed"},
    }
    assert (told["old_task"], told["task"]) == (None, None)  # restored: nothing changed stands
    assert next(r for r in rows if r["stop_reason"] == "inactive")["reported"] is False


async def test_live_records_come_first_then_the_newest(job_env):  # noqa: F811
    env = await job_env()
    for task, record in (
        ("kept old", {"state": "kept", "settled_at": ago(50)}),
        ("stopped new", {"state": "stopped", "settled_at": ago(1)}),
        ("running old", {"state": "running", "started_at": ago(40)}),
        ("stopping new", {"state": "stopping", "started_at": ago(3)}),
        ("closed mid", {"state": "closed", "started_at": ago(30), "settled_at": ago(10)}),
    ):
        await make_schedule(env, task=task, metadata={FIX_KEY: record})
    assert [r["job"] for r in await jobs(env)] == [
        "stopping new", "running old", "stopped new", "closed mid", "kept old",
    ]


async def test_malformed_values_are_dropped_and_long_text_is_clipped(job_env):  # noqa: F811
    env = await job_env()
    record = {
        "state": "judging", "started_at": "yesterday", "applied_at": 7, "fix_run": 12,
        "baseline": {"bad": 1, "scored": 0}, "report_id": "",
        "detail": {"why": "x" * (TEXT_CHARS + 50), "checked": 3, "diagnosis": "  ", "check": "ran"},
    }
    await make_schedule(env, metadata={FIX_KEY: record})
    await make_schedule(env, metadata={FIX_KEY: "not a record"})
    [row] = await jobs(env)
    assert (row["started_at"], row["applied_at"], row["fix_run"], row["baseline"]) == (None, None, None, None)
    assert row["since_change"] is None and row["reported"] is False
    assert row["detail"] == {"why": "x" * TEXT_CHARS + "..."}
