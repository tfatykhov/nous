"""End to end on real Postgres: a job that keeps failing gets one fix attempt, which is kept or undone."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest
import pytest_asyncio
from job_support import NOW, VALID, _fence, job_env, make_run, make_schedule  # noqa: F401
from sqlalchemy import delete, select, update

from nous.brain.intentions import IntentionSpec
from nous.handlers.job_fixes import JobFixer
from nous.storage.models import Intention, ResultInbox, Schedule, Subtask

pytestmark = [pytest.mark.postgres_only, pytest.mark.scoring_writer]

NEEDS_OWNER = _fence(VALID["needs_owner"])
CHANGED = _fence(VALID["changed_job"])


@pytest_asyncio.fixture
async def fix_base(job_env):  # noqa: F811
    """A job with an owner chat and a container, no runs yet. Teardown also removes its inbox rows and intentions."""
    env = await job_env(
        result_inbox_enabled=True, intentions_enabled=True, continuation_enabled=True, telegram_chat_id="8080"
    )
    schedule = await env.heart.schedules.create(
        task="Run the grid tick",
        schedule_type="recurring",
        interval_seconds=1800,
        timeout=120,
        intention=IntentionSpec(intent="Run the grid tick", origin_kind="interactive", container=True),
    )
    yield SimpleNamespace(env=env, schedule=schedule)
    async with env.db.session() as s:
        for model in (ResultInbox, Intention):
            await s.execute(delete(model).where(model.agent_id == env.agent))
        await s.commit()


@pytest_asyncio.fixture
async def fix_env(fix_base):
    """The job with five scored runs (three failed, two fine) and a fixer whose reviewer accepts."""
    env, schedule = fix_base.env, fix_base.schedule
    for hours, outcome in ((10, "failed"), (20, "failed"), (30, "failed"), (40, "done_self"), (50, "done_self")):
        await make_run(env, schedule, outcome=outcome, age_hours=hours)
    llm = AsyncMock()
    llm.call = AsyncMock(return_value=SimpleNamespace(content=[{"type": "text", "text": '{"verdict": "ACCEPT"}'}]))
    fixer = JobFixer(env.heart, env.settings, llm, clock=lambda: NOW)
    return SimpleNamespace(env=env, schedule=schedule, fixer=fixer, llm=llm)


async def fix_subtask(env) -> Subtask:
    """The agent's one fix run."""
    async with env.db.session() as s:
        rows = (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all()
    [sub] = [r for r in rows if (r.metadata_ or {}).get("job_fix")]
    return sub


async def inbox(env) -> list[ResultInbox]:
    """The agent's inbox rows."""
    async with env.db.session() as s:
        return list((await s.execute(select(ResultInbox).where(ResultInbox.agent_id == env.agent))).scalars())


async def complete(env, sub: Subtask, text: str) -> None:
    """Finish the fix run with ``text`` as its report."""
    await env.heart.subtasks.complete(sub.id, text, final_outcome="completed")


async def test_a_needs_owner_verdict_stops_the_job_and_tells_the_owner_once(fix_env):
    env, schedule, fixer = fix_env.env, fix_env.schedule, fix_env.fixer
    assert (await fixer.run_sleep())["started"] == 1
    sub = await fix_subtask(env)
    assert sub.metadata_["schedule_id"] == schedule.id.hex and sub.metadata_["job_fix"] is True
    assert sub.timeout_seconds == 600
    fire = await env.heart.intentions.get_for_source("subtask", sub.id)
    # the brief is not the intention's line
    assert fire.intent == "Check a background job that keeps going wrong: Run the grid tick"
    await complete(env, sub, "Checked.\n" + NEEDS_OWNER)
    assert (await fixer.run_sleep())["stopped"] == 1
    [row] = await inbox(env)
    assert row.source_kind == "intention_report" and row.msg_type == "REPORT" and row.channel == "telegram:8080"
    assert str(row.source_id) == str(uuid5(NAMESPACE_URL, f"nous-job-fix:{schedule.id}"))
    container = await env.heart.intentions.get_for_source("schedule", schedule.id)
    # the report row is linked to the job's container, which the stop closed afterwards
    assert row.intention_id == container.id and container.state == "closed"
    got = await env.heart.schedules.get(schedule.id)
    assert got.active is False and got.metadata_["job_fix"]["state"] == "stopped"
    assert got.metadata_["job_fix"]["stop_reason"] == "needs_owner"
    assert got.metadata_["job_fix"]["report_id"] == str(row.source_id)
    assert "run it under owner authority" in row.body and "Run the grid tick" in row.body
    assert (await fixer.run_sleep())["stopped"] == 0 and len(await inbox(env)) == 1  # no second row


async def test_an_accepted_change_that_does_not_help_is_rolled_back_then_the_job_is_stopped(fix_env):
    env, schedule, fixer = fix_env.env, fix_env.schedule, fix_env.fixer
    await fixer.run_sleep()
    sub = await fix_subtask(env)
    await complete(env, sub, "Fixed the text.\n" + CHANGED)
    assert (await fixer.run_sleep())["applied"] == 1
    got = await env.heart.schedules.get(schedule.id)
    assert got.task == "Run the check and report."
    assert got.metadata_["job_fix"]["old_task"] == "Run the grid tick"
    assert got.metadata_["job_fix"]["state"] == "judging"
    # five scored runs finishing after the change (negative age = later than NOW), three failed; one is still
    # provisional and the judge must count it
    for hours in (1, 2, 3, 4, 5):
        await make_run(
            env, schedule, outcome="failed" if hours <= 3 else "done_self", age_hours=-hours, settled=hours != 4
        )
    assert (await fixer.run_sleep())["stopped"] == 1
    got = await env.heart.schedules.get(schedule.id)
    assert got.task == "Run the grid tick" and got.active is False
    assert got.metadata_["job_fix"]["stop_reason"] == "did_not_help"
    assert len(await inbox(env)) == 1


async def test_a_job_the_owner_stops_while_a_fix_runs_ends_stopped_with_no_message(fix_env):
    env, schedule, fixer = fix_env.env, fix_env.schedule, fix_env.fixer
    await fixer.run_sleep()
    await env.heart.schedules.deactivate(schedule.id)
    stats = await fixer.run_sleep()
    got = await env.heart.schedules.get(schedule.id)
    assert (got.metadata_["job_fix"]["state"], got.metadata_["job_fix"]["stop_reason"]) == ("stopped", "inactive")
    assert await inbox(env) == [] and stats["stopped"] == 0
    # the fix run was cancelled with the job
    assert (await env.heart.subtasks.get((await fix_subtask(env)).id)).status == "cancelled"


async def test_a_scored_fix_run_is_never_a_run_of_the_job(fix_env):
    env, schedule, fixer = fix_env.env, fix_env.schedule, fix_env.fixer
    await fixer.run_sleep()
    sub = await fix_subtask(env)
    await complete(env, sub, "Fixed.\n" + _fence(VALID["fixed_elsewhere"]))
    await fixer.run_sleep()  # judging, applied_at is now
    for hours in (-0.1, -0.2, -0.3, -0.4):
        await make_run(env, schedule, outcome="done_self", age_hours=hours)  # four scored runs of the job
    fix_run = await make_run(env, schedule, outcome="failed", age_hours=-0.5, fix=True)  # not a fifth
    runs = await env.heart.job_runs.scored_runs(schedule.id.hex, settled_only=False)
    assert len(runs) == 9 and fix_run.id not in {r.subtask_id for r in runs}
    stats = await fixer.run_sleep()
    assert stats["waiting"] == 1 and stats["kept"] == 0 and stats["stopped"] == 0


async def test_a_malformed_job_fix_record_does_not_stop_the_sleep(fix_env):
    env, fixer = fix_env.env, fix_env.fixer
    odd = await make_schedule(env)
    async with env.db.session() as s:  # a non-dict record, written around set_metadata
        await s.execute(update(Schedule).where(Schedule.id == odd.id).values(metadata_={"job_fix": "garbage"}))
        await s.commit()
    stats = await fixer.run_sleep()
    assert stats["started"] == 1 and (await env.heart.schedules.get(odd.id)).metadata_ == {"job_fix": "garbage"}


async def test_a_crash_before_the_record_is_written_does_not_start_a_second_fix_run(fix_env):
    env, schedule, fixer = fix_env.env, fix_env.schedule, fix_env.fixer
    assert (await fixer.run_sleep())["started"] == 1
    first = await fix_subtask(env)
    async with env.db.session() as s:  # the crash: the run is committed, its record is not
        await s.execute(
            update(Schedule)
            .where(Schedule.id == schedule.id)
            .values(metadata_=Schedule.metadata_.op("-")("job_fix"))
        )
        await s.commit()
    assert "job_fix" not in ((await env.heart.schedules.get(schedule.id)).metadata_ or {})
    assert (await fixer.run_sleep())["started"] == 1
    async with env.db.session() as s:
        rows = (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all()
    assert len([r for r in rows if (r.metadata_ or {}).get("job_fix")]) == 1
    record = (await env.heart.schedules.get(schedule.id)).metadata_["job_fix"]
    assert record["fix_run"] == str(first.id) and record["state"] == "running"
    assert record["started_at"] == first.created_at.isoformat()
