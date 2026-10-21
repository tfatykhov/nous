"""Job lessons in the procedure store.

A job lesson is a procedure row with ``kind='job_lesson'`` learned from a
recurring background job's bad runs; ``runtime_metadata.job.schedule_id``
names its job. These tests pin the kind, the per-job reads (list, scoped
cosine), retiring with a reason, and that storing one emits no in-process
graph-linking event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from job_support import lesson_heart, lesson_input  # noqa: F401
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from nous.config import Settings
from nous.heart import Heart
from nous.heart.procedures import _build_embed_text
from nous.heart.schemas import JOB_LESSON_KIND, ProcedureInput, is_job_lesson
from nous.storage.models import Procedure

pytestmark = pytest.mark.postgres_only

HEX_A, HEX_B = uuid4().hex, uuid4().hex


class _Bus:
    """A bus that keeps what it was asked to emit."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _embed_text(p) -> str:
    return _build_embed_text(
        p.name, p.description, p.core_patterns, p.goals, p.core_tools, p.core_concepts, p.implementation_notes
    )


def test_is_job_lesson_reads_the_kind():
    assert JOB_LESSON_KIND == "job_lesson"
    assert is_job_lesson(ProcedureInput(name="x", kind="job_lesson")) is True
    assert is_job_lesson(ProcedureInput(name="x", kind="strategy")) is False
    assert is_job_lesson(ProcedureInput(name="x")) is False


async def test_list_job_lessons_filters_by_schedule_and_active_and_orders_newest_first(
    lesson_heart,  # noqa: F811
    session,
):
    h = lesson_heart
    old = await h.store_procedure(lesson_input(HEX_A, "[job a] old"), session)
    new = await h.store_procedure(lesson_input(HEX_A, "[job a] new"), session)
    other = await h.store_procedure(lesson_input(HEX_B, "[job b] other"), session)
    howto = await h.store_procedure(ProcedureInput(name="plain how-to", tags=["x"]), session)
    await session.execute(
        update(Procedure).where(Procedure.id == old.id).values(created_at=datetime.now(UTC) - timedelta(days=2))
    )
    await h.retire_procedure(other.id, session)
    assert [x.id for x in await h.list_job_lessons(HEX_A, session=session)] == [new.id, old.id]
    assert {x.id for x in await h.list_job_lessons(session=session)} == {new.id, old.id}  # B is retired, so not listed
    [retired] = await h.list_job_lessons(HEX_B, active=None, session=session)
    assert retired.id == other.id and retired.active is False
    first = (await h.list_job_lessons(HEX_A, session=session))[0]
    assert (first.cue, first.lesson, first.schedule_id) == (
        "the job meets a refusal",
        "ask for owner authority first",
        HEX_A,
    )
    assert first.baseline == {"bad": 1, "scored": 4, "window_end": "2026-10-01T00:00:00+00:00"}
    assert first.retired is None
    assert isinstance(first.learned_from_run, str) and first.created_at is not None
    every = await h.list_job_lessons(active=None, session=session)
    assert howto.id not in {x.id for x in every}


async def test_find_similar_job_lessons_is_scoped_to_one_schedule(lesson_heart, session):  # noqa: F811
    h = lesson_heart
    a = await h.store_procedure(lesson_input(HEX_A, "[job a] one"), session)
    b = await h.store_procedure(lesson_input(HEX_B, "[job b] two"), session)
    howto = await h.store_procedure(ProcedureInput(name="plain how-to"), session)
    row_a = await session.get(Procedure, a.id)
    for other in (b, howto):  # identical embeddings: only the scope can tell them apart
        (await session.get(Procedure, other.id)).embedding = row_a.embedding
    await session.flush()
    query = _embed_text(a)
    found = await h.find_similar_job_lessons(query, HEX_A, limit=3, session=session)
    assert [f.id for f in found] == [a.id] and found[0].score > 0.99 and found[0].kind == "job_lesson"
    assert [f.id for f in await h.find_similar_job_lessons(query, HEX_B, session=session)] == [b.id]
    assert await h.find_similar_job_lessons(query, uuid4().hex, session=session) == []


async def test_find_similar_job_lessons_is_empty_without_an_embedding_provider(db):
    settings = Settings().model_copy(update={"agent_id": f"job-noembed-{uuid4().hex[:8]}"})
    h = Heart(db, settings, embedding_provider=None)
    try:
        assert await h.find_similar_job_lessons("anything", HEX_A) == []
    finally:
        await h.close()


async def test_retire_with_a_reason_keeps_the_job_record_and_adds_the_reason(lesson_heart, session):  # noqa: F811
    h = lesson_heart
    lesson = await h.store_procedure(lesson_input(HEX_A, "[job a] one"), session)
    reason = {"reason": "no_improvement", "at": "2026-10-22T00:00:00+00:00", "after": {"bad": 2, "scored": 5}}
    await h.retire_procedure(lesson.id, session, reason=reason)
    row = await session.get(Procedure, lesson.id)
    await session.refresh(row)  # what the database holds, not the session's copy
    assert row.active is False and row.runtime_metadata["retired"] == reason
    assert row.runtime_metadata["job"]["schedule_id"] == HEX_A
    assert await h.list_job_lessons(HEX_A, session=session) == []
    [gone] = await h.list_job_lessons(HEX_A, active=False, session=session)
    assert gone.retired == reason


async def test_retire_without_a_reason_leaves_runtime_metadata_alone(lesson_heart, session):  # noqa: F811
    h = lesson_heart
    lesson = await h.store_procedure(lesson_input(HEX_A, "[job a] one"), session)
    before = dict((await session.get(Procedure, lesson.id)).runtime_metadata)
    await h.retire_procedure(lesson.id, session)
    row = await session.get(Procedure, lesson.id)
    await session.refresh(row)
    assert row.active is False and row.runtime_metadata == before and "retired" not in row.runtime_metadata


async def test_a_job_lesson_emits_no_in_process_procedure_event(lesson_heart, session):  # noqa: F811
    h = lesson_heart
    h._bus = _Bus()
    lesson = await h.store_procedure(lesson_input(HEX_A, "[job a] one"), session)
    await h.update_procedure_body(lesson.id, lesson_input(HEX_A, "[job a] one", lesson="changed"), session)
    assert h._bus.events == []
    await h.store_procedure(ProcedureInput(name="plain how-to"), session)
    assert [e.type for e in h._bus.events] == ["procedure_stored"]


async def test_a_job_lesson_stores_beside_an_active_procedure_with_the_model_chosen_name(lesson_heart):  # noqa: F811
    h = lesson_heart  # committed rows: the unique index is checked at commit
    run_a, run_b = uuid4(), uuid4()
    await h.store_procedure(ProcedureInput(name="Shell refusal"))
    first = await h.store_procedure(lesson_input(HEX_A, f"[job {HEX_A[:8]}] Shell refusal ({str(run_a)[:8]})"))
    second = await h.store_procedure(lesson_input(HEX_A, f"[job {HEX_A[:8]}] Shell refusal ({str(run_b)[:8]})"))
    assert first.name != second.name != "Shell refusal"
    assert {x.id for x in await h.list_job_lessons(HEX_A)} == {first.id, second.id}
    with pytest.raises(IntegrityError):  # the same name twice is what the suffix prevents
        await h.store_procedure(lesson_input(HEX_A, first.name))


async def test_a_job_lesson_never_renames_a_strategy_card(lesson_heart):  # noqa: F811
    h = lesson_heart  # committed rows: the rejected insert rolls back its own session only
    card = await h.store_procedure(ProcedureInput(name="Shell refusal", kind="strategy"))
    with pytest.raises(IntegrityError):  # the card keeps the name, so the unique index refuses the lesson
        await h.store_procedure(lesson_input(HEX_A, "Shell refusal"))
    kept = await h.procedures.get(card.id)
    assert kept.name == "Shell refusal" and kept.active


async def test_a_malformed_job_record_is_skipped_and_does_not_break_the_list(lesson_heart, session):  # noqa: F811
    h = lesson_heart
    good = await h.store_procedure(lesson_input(HEX_A, "[job a] fine"), session)
    broken = lesson_input(HEX_A, "[job a] broken")
    broken.runtime_metadata = {"job": "not a dict"}
    await h.store_procedure(broken, session)
    assert [x.id for x in await h.list_job_lessons(session=session)] == [good.id]
