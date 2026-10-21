"""End-to-end guard: scorer output never reaches a prompt, a stored lesson or the lessons block."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from job_support import assert_no_scorer_text, job_env, make_run, make_schedule  # noqa: F401

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
        "name", "description", "goals", "core_patterns", "core_tools", "core_concepts",
        "implementation_notes", "tags", "runtime_metadata",
    )
    return {n: getattr(row, n, None) for n in names}


async def test_no_scorer_output_reaches_a_prompt_a_stored_lesson_or_the_block(job_env):  # noqa: F811
    env = await job_env(job_lessons_enabled=True)
    schedule = await make_schedule(env, task="Check the pre-market report")
    sentinel = {"note": "SENTINEL-EVIDENCE-7f3a"}
    bad = await make_run(
        env, schedule, outcome="partial", age_hours=3, final_outcome="incomplete_blocked",
        blocked_reason="the shell refused the script", evidence=sentinel, score=0.123456, ledger=[("bash", "blocked")],
    )
    good = [
        await make_run(
            env, schedule, outcome=o, age_hours=h, evidence=sentinel, score=0.123456,
            ledger=[("write_file", "success")],
        )
        for o, h in (("done_self", 30), ("done_verified", 50), ("done_checked", 70))
    ]
    llm = _CapturingLLM(answer={
        "cue": "The script is refused by the shell", "lesson": "Report the refusal instead of retrying the script",
        "evidence": [{"id": str(bad.id), "outcome": "refused"}, {"id": str(good[0].id), "outcome": "ran"}],
        "name": "Shell refusal", "domain": "ops", "description": "Handle a refused script",
    })
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
