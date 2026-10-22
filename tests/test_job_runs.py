"""The run reader: which runs of a recurring background job come back, chosen by their score.

The score only decides which runs are returned; each returned run is the run's own record.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from job_support import NOW, job_env, make_run, make_schedule  # noqa: F401

from nous.heart.job_runs import BAD_OUTCOMES, GOOD_OUTCOMES
from nous.observability.task_scoring import OUTCOMES, SCORER_VERSION

pytestmark = [pytest.mark.postgres_only, pytest.mark.scoring_writer]


def test_every_outcome_is_good_bad_or_not_scored():
    assert set(BAD_OUTCOMES) | set(GOOD_OUTCOMES) | {"not_scored"} == set(OUTCOMES)
    assert not set(BAD_OUTCOMES) & set(GOOD_OUTCOMES)


async def test_lesson_cases_are_the_newest_bad_run_of_each_active_recurring_schedule(job_env):  # noqa: F811
    env = await job_env()
    r = env.heart.job_runs
    a, b = await make_schedule(env), await make_schedule(env)
    once, off = await make_schedule(env, schedule_type="once"), await make_schedule(env, active=False)
    await make_run(env, a, outcome="failed", age_hours=30)
    a_new = await make_run(env, a, outcome="partial", age_hours=5)
    b_bad = await make_run(env, b, outcome="failed", age_hours=10)
    await make_run(env, once, outcome="failed", age_hours=1)
    await make_run(env, off, outcome="failed", age_hours=1)
    await make_run(env, a, outcome="done_self", age_hours=2)
    cases = await r.lesson_cases(since=NOW - timedelta(days=30), limit=10)
    assert [c.subtask_id for c in cases] == [a_new.id, b_bad.id] and all(c.bad for c in cases)
    assert [c.subtask_id for c in await r.lesson_cases(since=NOW - timedelta(days=30), limit=1)] == [a_new.id]
    assert await r.lesson_cases(since=NOW - timedelta(hours=1), limit=10) == []
    assert [c.subtask_id for c in await r.lesson_cases(since=NOW - timedelta(days=30), limit=None)] == [
        a_new.id, b_bad.id
    ]


async def test_lesson_cases_respect_reviewed_until(job_env):  # noqa: F811
    env = await job_env()
    r = env.heart.job_runs
    s = await make_schedule(env)
    run = await make_run(env, s, outcome="failed", age_hours=5)
    since = NOW - timedelta(days=30)
    await env.heart.schedules.set_metadata(s.id, "job_lessons", {"reviewed_until": run.finished_at.isoformat()})
    assert await r.lesson_cases(since=since, limit=10) == []
    await env.heart.schedules.set_metadata(
        s.id, "job_lessons", {"reviewed_until": (run.finished_at - timedelta(hours=1)).isoformat()}
    )
    assert [c.subtask_id for c in await r.lesson_cases(since=since, limit=10)] == [run.id]
    # a run scored late, finished before reviewed_until, is skipped by the same rule (accepted)
    await make_run(env, s, outcome="partial", age_hours=8)
    assert [c.subtask_id for c in await r.lesson_cases(since=since, limit=10)] == [run.id]


@pytest.mark.parametrize(
    "meta",
    [
        "garbage",
        ["x"],
        {"reviewed_until": "not a date"},
        {"reviewed_until": "2026-10-01T00:00:00"},
        {"reviewed_until": 5},
        {"reviewed_until": None, "retry": "junk"},
        {"retry": {"run": 1, "n": "x"}},
    ],
)
async def test_malformed_schedule_metadata_is_treated_as_absent(job_env, meta):  # noqa: F811
    env = await job_env()
    s = await make_schedule(env, metadata={"job_lessons": meta})
    run = await make_run(env, s, outcome="failed", age_hours=3)
    got = await env.heart.job_runs.lesson_cases(since=NOW - timedelta(days=30), limit=10)
    assert [c.subtask_id for c in got] == [run.id]


async def test_a_malformed_subtask_schedule_id_does_not_raise(job_env):  # noqa: F811
    env = await job_env()
    r = env.heart.job_runs
    s = await make_schedule(env)
    await make_run(env, s, outcome="failed", age_hours=2, metadata={"schedule_id": "not-a-uuid"})
    good = await make_run(env, s, outcome="failed", age_hours=3)
    assert await r.scored_runs("not-a-uuid") == []
    assert [x.subtask_id for x in await r.scored_runs(s.id.hex)] == [good.id]
    assert [c.subtask_id for c in await r.lesson_cases(since=NOW - timedelta(days=30), limit=10)] == [good.id]


async def test_fix_runs_are_excluded_everywhere(job_env):  # noqa: F811
    env = await job_env()
    r = env.heart.job_runs
    s = await make_schedule(env)
    await make_run(env, s, outcome="failed", age_hours=1, fix=True)
    await make_run(env, s, outcome="done_self", age_hours=2, fix=True)
    since = NOW - timedelta(days=30)
    assert await r.lesson_cases(since=since, limit=10) == []
    assert await r.schedules_with_bad_runs(since=since) == []
    assert await r.scored_runs(s.id.hex) == [] and await r.good_runs(s.id.hex, before=NOW, since=since) == []
    seen = await make_run(env, s, outcome="failed", age_hours=3)  # not vacuous: a normal run is visible everywhere
    assert [c.subtask_id for c in await r.lesson_cases(since=since, limit=10)] == [seen.id]
    assert [x.subtask_id for x in await r.scored_runs(s.id.hex)] == [seen.id]


async def test_scored_runs_leave_out_not_scored_other_agents_and_other_versions(job_env):  # noqa: F811
    env = await job_env()
    other = await job_env()
    r = env.heart.job_runs
    s = await make_schedule(env)
    inside = [await make_run(env, s, outcome="failed", age_hours=h) for h in (2, 4, 6)]  # newest first
    await make_run(env, s, outcome="not_scored", age_hours=1)
    await make_run(env, s, outcome="failed", age_hours=1, version=SCORER_VERSION - 1)
    await make_run(other, s, outcome="failed", age_hours=1)  # another agent, same schedule hex
    assert [x.subtask_id for x in await r.scored_runs(s.id.hex)] == [x.id for x in inside]
    assert [x.subtask_id for x in await r.scored_runs(s.id.hex, limit=2)] == [inside[0].id, inside[1].id]
    after = inside[2].finished_at
    assert [x.subtask_id for x in await r.scored_runs(s.id.hex, after=after)] == [inside[0].id, inside[1].id]
    got = await r.scored_runs(s.id.hex, until=inside[1].finished_at)  # until inclusive
    assert [x.subtask_id for x in got] == [inside[1].id, inside[2].id]
    assert all(x.bad for x in await r.scored_runs(s.id.hex))


async def test_good_runs_window_and_order(job_env):  # noqa: F811
    env = await job_env()
    r = env.heart.job_runs
    s = await make_schedule(env)
    spec = (("done_self", 10), ("done_verified", 20), ("done_checked", 30), ("done_self", 40))
    ins = [await make_run(env, s, outcome=o, age_hours=h) for o, h in spec]
    at_before = await make_run(env, s, outcome="done_self", age_hours=5)
    await make_run(env, s, outcome="done_self", age_hours=100)  # before `since`
    await make_run(env, s, outcome="failed", age_hours=15)  # not a good outcome
    before, since = at_before.finished_at, NOW - timedelta(hours=50)
    got = await r.good_runs(s.id.hex, before=before, since=since)  # [since, before), newest first, limit 3
    assert [x.subtask_id for x in got] == [x.id for x in ins[:3]] and at_before.id not in {x.subtask_id for x in got}
    assert [x.subtask_id for x in await r.good_runs(s.id.hex, before=before, since=since, limit=4)] == [
        x.id for x in ins
    ]


async def test_lesson_reads_ignore_provisional_scores(job_env):  # noqa: F811
    env = await job_env()
    r = env.heart.job_runs
    s = await make_schedule(env)
    since = NOW - timedelta(days=30)
    bad = await make_run(env, s, outcome="failed", age_hours=5, settled=False)
    good = await make_run(env, s, outcome="done_self", age_hours=8, settled=False)
    assert await r.lesson_cases(since=since, limit=10) == []
    assert await r.good_runs(s.id.hex, before=NOW, since=since) == []
    assert await r.scored_runs(s.id.hex) == []
    assert {x.subtask_id for x in await r.scored_runs(s.id.hex, settled_only=False)} == {bad.id, good.id}
    assert await r.schedules_with_bad_runs(since=since) == [s.id.hex]
    t = await make_schedule(env)
    bad_t = await make_run(env, t, outcome="failed", age_hours=5)
    good_t = await make_run(env, t, outcome="done_self", age_hours=8)
    assert [c.subtask_id for c in await r.lesson_cases(since=since, limit=10)] == [bad_t.id]
    assert [g.subtask_id for g in await r.good_runs(t.id.hex, before=NOW, since=since)] == [good_t.id]
    assert [x.subtask_id for x in await r.scored_runs(t.id.hex)] == [bad_t.id, good_t.id]
    assert bad.id != bad_t.id and good.id != good_t.id


async def test_schedules_with_bad_runs(job_env):  # noqa: F811
    env = await job_env()
    r = env.heart.job_runs
    a, b, c = await make_schedule(env), await make_schedule(env), await make_schedule(env, active=False)
    await make_run(env, a, outcome="failed", age_hours=2)
    await make_run(env, a, outcome="partial", age_hours=3)
    await make_run(env, b, outcome="done_self", age_hours=2)
    await make_run(env, c, outcome="failed", age_hours=2)
    assert await r.schedules_with_bad_runs(since=NOW - timedelta(days=1)) == [a.id.hex]
    assert await r.schedules_with_bad_runs(since=NOW - timedelta(hours=1)) == []


async def test_ledger_calls_order_cap_and_agent_scope(job_env):  # noqa: F811
    env = await job_env()
    other_env = await job_env()
    r = env.heart.job_runs
    s = await make_schedule(env)
    ledger = [("bash", "error"), ("write_file", "success"), ("send_message", "success")]
    run = await make_run(env, s, outcome="failed", age_hours=1, ledger=ledger)
    quiet = await make_run(env, s, outcome="done_self", age_hours=2)
    other_s = await make_schedule(other_env)
    foreign = await make_run(other_env, other_s, outcome="failed", age_hours=1, ledger=[("bash", "error")])
    calls = await r.ledger_calls([run.id, quiet.id, foreign.id], per_run=2)
    assert calls[run.id] == [("bash", "error"), ("write_file", "success")]  # oldest first, capped at 2 of 3
    assert calls[quiet.id] == [] and calls[foreign.id] == []  # no entries, and another agent's are not read
