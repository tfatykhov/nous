"""End-to-end guard: scorer output never reaches a prompt, a stored lesson or the lessons block."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from job_support import NOW, assert_no_scorer_text, job_env, lesson_input, make_run, make_schedule  # noqa: F401
from sqlalchemy import update

from nous.handlers.job_lessons import JobLessonLearner, job_lessons_block
from nous.handlers.procedure_learner import ProcedureLearner
from nous.storage.models import Procedure

pytestmark = [pytest.mark.postgres_only, pytest.mark.scoring_writer]


class _CapturingLLM:
    """Fake LLM client: records every payload and answers with a fixed JSON object."""

    def __init__(self, answer: dict) -> None:
        self.payloads: list[dict] = []
        self._answer = answer

    async def call(self, payload: dict) -> SimpleNamespace:
        self.payloads.append(payload)
        return SimpleNamespace(content=[{"type": "text", "text": json.dumps(self._answer)}])

    def texts(self) -> list[str]:
        return [json.dumps(p, default=str) for p in self.payloads]


async def _row(env, procedure_id: UUID) -> Procedure:
    async with env.db.session() as s:
        return await s.get(Procedure, procedure_id)


def _row_fields(row: Procedure) -> dict:
    names = (
        "name",
        "description",
        "goals",
        "core_patterns",
        "core_tools",
        "core_concepts",
        "implementation_notes",
        "tags",
        "runtime_metadata",
    )
    return {n: getattr(row, n, None) for n in names}


async def test_no_scorer_output_reaches_a_prompt_a_stored_lesson_or_the_block(job_env):  # noqa: F811
    env = await job_env(job_lessons_enabled=True)
    schedule = await make_schedule(env, task="Check the pre-market report")
    sentinel = {"note": "SENTINEL-EVIDENCE-7f3a"}
    bad = await make_run(
        env,
        schedule,
        outcome="partial",
        age_hours=3,
        final_outcome="incomplete_blocked",
        blocked_reason="the shell refused the script",
        evidence=sentinel,
        score=0.123456,
        ledger=[("bash", "blocked")],
    )
    good = [
        await make_run(
            env,
            schedule,
            outcome=o,
            age_hours=h,
            evidence=sentinel,
            score=0.123456,
            ledger=[("write_file", "success")],
        )
        for o, h in (("done_self", 30), ("done_verified", 50), ("done_checked", 70))
    ]
    llm = _CapturingLLM(
        answer={
            "cue": "The script is refused by the shell",
            "lesson": "Report the refusal instead of retrying the script",
            "evidence": [{"id": str(bad.id), "outcome": "refused"}, {"id": str(good[0].id), "outcome": "ran"}],
            "name": "Shell refusal",
            "domain": "ops",
            "description": "Handle a refused script",
        }
    )
    learner = ProcedureLearner(AsyncMock(), env.heart, AsyncMock(), env.settings, llm)
    stats = await JobLessonLearner(learner, env.heart, env.settings).run_sleep()
    assert stats["learned"] == 1  # the pathway really ran
    [stored] = await env.heart.list_job_lessons(schedule.id.hex)
    row = await _row(env, stored.id)
    scheduled_run = SimpleNamespace(metadata_={"schedule_id": schedule.id.hex})
    block = await job_lessons_block(env.heart, env.settings, scheduled_run)
    assert "incomplete_blocked" in llm.texts()[0] and block is not None
    assert_no_scorer_text(*llm.texts(), json.dumps(_row_fields(row), default=str), block)


def test_the_guard_catches_a_leak_and_only_a_whole_word_partial():
    for leak in ("x done_self y", "score 0.123456", "SENTINEL-EVIDENCE-7f3a", "outcome: not_scored", "it was partial"):
        with pytest.raises(AssertionError):
            assert_no_scorer_text(leak)
    assert_no_scorer_text("partially blocked")  # the \bpartial\b boundary is deliberate


async def _store_aged_lesson(env, schedule) -> Procedure:
    """A real job lesson (baseline 1 bad of 10 scored runs) stored ten days before the test runs' clock."""
    item = lesson_input(schedule.id.hex, "[job] lesson")
    item.runtime_metadata["job"]["baseline"] = {"bad": 1, "scored": 10, "window_end": NOW.isoformat()}
    stored = await env.heart.store_procedure(item)
    async with env.db.session() as s:
        await s.execute(update(Procedure).where(Procedure.id == stored.id).values(created_at=NOW - timedelta(days=10)))
        await s.commit()
    return await _row(env, stored.id)


async def _judge_only(env) -> dict[str, int]:
    settings = env.settings.model_copy(update={"job_lessons_per_sleep": 0})
    learner = ProcedureLearner(AsyncMock(), env.heart, AsyncMock(), settings, _CapturingLLM({}))
    return await JobLessonLearner(learner, env.heart, settings).run_sleep()


async def test_the_retire_rule_retires_a_lesson_whose_job_did_not_improve(job_env):  # noqa: F811
    env = await job_env(job_lessons_enabled=True)
    schedule = await make_schedule(env)
    lesson = await _store_aged_lesson(env, schedule)
    for hours in range(1, 6):
        await make_run(env, schedule, outcome="failed", age_hours=hours, final_outcome="failed", status="failed")
    stats = await _judge_only(env)
    assert (stats["retired"], stats["kept"]) == (1, 0)
    row = await _row(env, lesson.id)
    assert row.active is False
    assert row.runtime_metadata["retired"]["reason"] == "no_improvement"
    assert row.runtime_metadata["job"]["schedule_id"] == schedule.id.hex  # the job record survives


async def test_the_retire_rule_keeps_a_lesson_whose_job_improved(job_env):  # noqa: F811
    env = await job_env(job_lessons_enabled=True)
    schedule = await make_schedule(env)
    lesson = await _store_aged_lesson(env, schedule)
    for hours in range(1, 6):
        await make_run(env, schedule, outcome="done_verified", age_hours=hours)
    stats = await _judge_only(env)
    assert (stats["retired"], stats["kept"]) == (0, 1)
    row = await _row(env, lesson.id)
    assert row.active is True and "retired" not in (row.runtime_metadata or {})


async def test_no_scorer_output_reaches_the_fix_brief_the_change_review_or_the_owner_message(job_env):  # noqa: F811
    from job_support import VALID, _fence
    from sqlalchemy import select

    from nous.brain.intentions import IntentionSpec
    from nous.handlers.job_fixes import JobFixer
    from nous.storage.models import Intention, ResultInbox, Subtask, TaskScore

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
    sentinel = {"note": "SENTINEL-EVIDENCE-7f3a"}
    run_kw = {"evidence": sentinel, "score": 0.123456}
    for hours, outcome in ((10, "failed"), (20, "failed"), (30, "failed"), (40, "done_self"), (50, "done_self")):
        bad = outcome == "failed"
        await make_run(
            env,
            schedule,
            outcome=outcome,
            age_hours=hours,
            status="failed" if bad else "completed",
            final_outcome="incomplete_blocked" if bad else "completed",
            blocked_reason="the shell refused the script" if bad else None,
            ledger=[("bash", "blocked")] if bad else [("write_file", "success")],
            **run_kw,
        )
    llm = _CapturingLLM({"verdict": "ACCEPT"})
    fixer = JobFixer(env.heart, env.settings, llm, clock=lambda: NOW)
    assert (await fixer.run_sleep())["started"] == 1
    async with env.db.session() as s:
        subs = (await s.execute(select(Subtask).where(Subtask.agent_id == env.agent))).scalars().all()
        [fix_sub] = [r for r in subs if (r.metadata_ or {}).get("job_fix")]
        # the fix run's own score row, written directly with sentinel values
        s.add(
            TaskScore(
                agent_id=env.agent,
                task_kind="subtask",
                task_id=str(fix_sub.id),
                finished_at=NOW - timedelta(hours=1),
                scorer_version=1,
                outcome="partial",
                score=0.123456,
                evidence=sentinel,
                settled=True,
            )
        )
        await s.commit()
        brief = fix_sub.task
        fix_id = fix_sub.id
    await make_run(env, schedule, outcome="failed", age_hours=-0.5, fix=True, **run_kw)  # a second, scored fix run
    await env.heart.subtasks.complete(
        fix_id, "Fixed the text.\n" + _fence(VALID["changed_job"]), final_outcome="completed"
    )
    assert (await fixer.run_sleep())["applied"] == 1
    for hours in (1, 2, 3, 4, 5):
        await make_run(
            env,
            schedule,
            outcome="failed" if hours <= 3 else "done_self",
            age_hours=-hours,
            status="failed" if hours <= 3 else "completed",
            final_outcome="incomplete_blocked" if hours <= 3 else "completed",
            **run_kw,
        )
    assert (await fixer.run_sleep())["stopped"] == 1
    async with env.db.session() as s:
        [row] = (await s.execute(select(ResultInbox).where(ResultInbox.agent_id == env.agent))).scalars().all()
    restored = (await env.heart.schedules.get(schedule.id)).task
    assert "incomplete_blocked" in brief and "Run the grid tick" in row.body
    assert llm.payloads, "the change review ran"
    assert_no_scorer_text(brief, *llm.texts(), row.title, row.body, restored)
    async with env.db.session() as s:  # clean up what the generic teardown does not know
        from sqlalchemy import delete

        for model in (ResultInbox, Intention):
            await s.execute(delete(model).where(model.agent_id == env.agent))
        await s.commit()
