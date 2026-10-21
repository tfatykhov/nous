"""Shared builders for the job-lesson tests.

The fixture is imported by name into a test module (``# noqa: F401``); the
builder is a plain function. Every fixture gets its own agent id, so tests
never see each other's rows.
"""

from __future__ import annotations

from uuid import uuid4

import pytest_asyncio
from conftest import MockEmbeddingProvider
from sqlalchemy import delete

from nous.config import Settings
from nous.heart import Heart
from nous.heart.schemas import JOB_LESSON_KIND, ProcedureInput
from nous.storage.models import Event, GraphEdge, Procedure


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
