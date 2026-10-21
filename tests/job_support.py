"""Shared builders for the job-lesson tests.

The fixture is imported by name into a test module (``# noqa: F401``); the
builder is a plain function. Every fixture gets its own agent id, so tests
never see each other's rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio
from conftest import MockEmbeddingProvider
from sqlalchemy import delete

from nous.config import Settings
from nous.heart import Heart
from nous.heart.job_runs import FIX_KEY
from nous.heart.schemas import JOB_LESSON_KIND, ProcedureInput
from nous.observability.task_scoring import SCORER_VERSION
from nous.storage.models import (
    Event,
    ExecutionLedgerEntry,
    GraphEdge,
    Procedure,
    Schedule,
    Subtask,
    TaskScore,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def lesson_heart(db):
    """A Heart on a fresh agent id with the mock embedder; deletes the agent's committed rows on teardown."""
    settings = Settings().model_copy(update={"agent_id": f"job-{uuid4().hex[:8]}"})
    h = Heart(db, settings, embedding_provider=MockEmbeddingProvider())
    yield h
    async with db.session() as s:
        for model in (Procedure, Event, GraphEdge):
            await s.execute(delete(model).where(model.agent_id == h.agent_id))
        await s.commit()
    await h.close()


def lesson_input(
    schedule_hex: str,
    name: str,
    *,
    cue: str = "the job meets a refusal",
    lesson: str = "ask for owner authority first",
    body: str = "",
) -> ProcedureInput:
    """A job lesson for the schedule ``schedule_hex``, shaped as the learner stores one."""
    return ProcedureInput(
        name=name,
        kind=JOB_LESSON_KIND,
        tags=["auto:job_lesson"],
        goals=[cue],
        implementation_notes=[f"lesson: {lesson}", "evidence: x (completed) — ok"],
        description=body or None,
        runtime_metadata={
            "job": {
                "schedule_id": schedule_hex,
                "learned_from_run": str(uuid4()),
                "baseline": {"bad": 1, "scored": 4, "window_end": "2026-10-01T00:00:00+00:00"},
            }
        },
    )


@pytest_asyncio.fixture
async def job_env(db, mock_embeddings):
    """Factory: ``env = await job_env(**settings_overrides)`` gives a namespace ``agent, settings, heart, db``.

    Each env has its own agent id. Teardown deletes the agent's committed rows; ``TaskScore`` rows only when
    ``make_run`` inserted one (``env.scored``), because the DB-roles CI job's app role cannot write that table.
    """
    envs: list[SimpleNamespace] = []

    async def make(**over) -> SimpleNamespace:
        agent = f"job-{uuid4().hex[:8]}"
        settings = Settings(_env_file=None, agent_id=agent, telegram_bot_token="", telegram_chat_id="", **over)
        heart = Heart(db, settings, embedding_provider=mock_embeddings)
        env = SimpleNamespace(agent=agent, settings=settings, heart=heart, db=db, scored=False)
        envs.append(env)
        return env

    yield make
    for env in envs:
        async with db.session() as s:
            models = [Event, Procedure, Schedule, Subtask, ExecutionLedgerEntry]
            if env.scored:
                models.append(TaskScore)
            for model in models:
                await s.execute(delete(model).where(model.agent_id == env.agent))
            await s.commit()
        await env.heart.close()


async def make_schedule(
    env, *, task: str = "Check the report", active: bool = True, schedule_type: str = "recurring",
    metadata: dict | None = None,
) -> Schedule:
    """Create a schedule for ``env``'s agent; ``metadata`` keys are written through ``set_metadata``."""
    if schedule_type == "once":
        sched = await env.heart.schedules.create(
            task=task, schedule_type="once", fire_at=datetime.now(UTC) + timedelta(days=30)
        )
    else:
        sched = await env.heart.schedules.create(task=task, schedule_type=schedule_type, interval_seconds=3600)
    if not active:
        await env.heart.schedules.deactivate(sched.id)
    for key, value in (metadata or {}).items():
        await env.heart.schedules.set_metadata(sched.id, key, value)
    return sched


async def make_run(
    env, schedule: Schedule, *, outcome: str, age_hours: float, status: str = "completed",
    final_outcome: str | None = "completed", result: str | None = "done", error: str | None = None,
    blocked_reason: str | None = None, evidence: dict | None = None, score: float | None = None,
    fix: bool = False, ledger: Sequence[tuple[str, str]] = (), metadata: dict | None = None,
    version: int = SCORER_VERSION,
) -> SimpleNamespace:
    """Insert one finished run of ``schedule`` with its score row and ledger entries (oldest first)."""
    finished_at = NOW - timedelta(hours=age_hours)
    async with env.db.session() as s:
        sub = Subtask(
            agent_id=env.agent, task="t", status=status, final_outcome=final_outcome, result=result, error=error,
            report_jsonb={"blocked_reason": blocked_reason} if blocked_reason else None,
            metadata_={"schedule_id": schedule.id.hex, **({FIX_KEY: True} if fix else {}), **(metadata or {})},
        )
        s.add(sub)
        await s.flush()
        s.add(TaskScore(
            agent_id=env.agent, task_kind="subtask", task_id=str(sub.id), finished_at=finished_at,
            scorer_version=version, outcome=outcome, score=score, evidence=evidence or {},
        ))
        env.scored = True
        for i, (tool, st) in enumerate(ledger):
            s.add(ExecutionLedgerEntry(
                agent_id=env.agent, context_kind="scheduled", side_effect_type="write", subtask_id=sub.id,
                tool_name=tool, status=st, created_at=finished_at - timedelta(minutes=len(ledger) - i),
            ))
        await s.commit()
        sub_id = sub.id
    return SimpleNamespace(id=sub_id, finished_at=finished_at)
