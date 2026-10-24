"""F102: procedure version history, optimistic concurrency, restore, and create-time dedupe.

Postgres only: the semantic merge needs pgvector, and the SQLite backend reads a version
row's array columns back as JSON text. Each test uses the SAVEPOINT session from conftest.py.
"""

import math

import pytest
from sqlalchemy import select

from nous.heart import ProcedureConflictError, ProcedureInput
from nous.heart.procedures import ProcedureManager
from nous.storage.models import Procedure, ProcedureVersion

pytestmark = pytest.mark.postgres_only


# Only this file's rows (each test rolls back) carry it: other files commit rows for the same agent.
_DOMAIN = "f102-versioning-test"


def _input(**overrides) -> ProcedureInput:
    defaults = dict(
        name="Report send results honestly",
        domain=_DOMAIN,
        description="Say a send was accepted, not delivered",
        goals=["cue: a send tool returned ok"],
        core_patterns=["accepted is not delivered"],
        implementation_notes=["lesson: report acceptance only"],
        tags=["auto:episode_lesson"],
    )
    defaults.update(overrides)
    return ProcedureInput(**defaults)


class _AxisEmbeddings:
    """Embeds a text to a unit vector at a chosen angle from axis 0, by the marker it contains.

    A text containing ``marker`` maps to cos(angle)·e0 + sin(angle)·e1, so two texts with
    markers at angles a and b have cosine cos(a - b). Unknown texts map to e2 (orthogonal).
    """

    def __init__(self, angles: dict[str, float]) -> None:
        self.angles = angles

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * 1536
        for marker, angle in self.angles.items():
            if marker in text:
                vec[0], vec[1] = math.cos(angle), math.sin(angle)
                return vec
        vec[2] = 1.0
        return vec

    async def close(self) -> None:
        pass


def _angle_for(cosine: float) -> float:
    return math.acos(cosine)


async def _versions(session, procedure_id) -> list[ProcedureVersion]:
    rows = await session.execute(
        select(ProcedureVersion).where(ProcedureVersion.procedure_id == procedure_id).order_by(ProcedureVersion.version)
    )
    return list(rows.scalars().all())


async def _active_count(session, agent_id: str) -> int:
    rows = await session.execute(
        select(Procedure.id)
        .where(Procedure.agent_id == agent_id)
        .where(Procedure.domain == _DOMAIN)
        .where(Procedure.active == True)  # noqa: E712
    )
    return len(rows.all())


# ---------------------------------------------------------------------------
# Versioning on every body writer
# ---------------------------------------------------------------------------


async def test_store_records_version_one(heart, session):
    detail = await heart.store_procedure(_input(), session=session)

    assert detail.version == 1
    versions = await heart.procedures.list_versions(detail.id, session=session)
    assert [(v.version, v.change_source) for v in versions] == [(1, "create")]
    assert versions[0].name == "Report send results honestly"
    assert versions[0].implementation_notes == ["lesson: report acceptance only"]


async def test_update_body_bumps_version_and_keeps_the_prior_body(heart, session):
    detail = await heart.store_procedure(_input(), session=session)

    updated = await heart.update_procedure_body(
        detail.id, _input(description="Rewritten body", core_patterns=["new pattern"]), session=session
    )

    assert updated.version == 2
    versions = await heart.procedures.list_versions(detail.id, session=session)
    assert [(v.version, v.change_source) for v in versions] == [(1, "create"), (2, "update_body")]
    assert versions[0].description == "Say a send was accepted, not delivered"
    assert versions[0].core_patterns == ["accepted is not delivered"]
    assert versions[1].description == "Rewritten body"


async def test_append_notes_bumps_version_only_when_notes_are_appended(heart, session):
    detail = await heart.store_procedure(_input(), session=session)

    assert await heart.append_procedure_notes(detail.id, lambda notes: [], session=session) == []
    assert (await heart.get_procedure(detail.id, session=session)).version == 1

    await heart.append_procedure_notes(detail.id, lambda notes: ["evidence: e1 (success) — ok"], session=session)

    after = await heart.get_procedure(detail.id, session=session)
    assert after.version == 2
    versions = await heart.procedures.list_versions(detail.id, session=session)
    assert versions[-1].change_source == "append_notes"
    assert versions[-1].implementation_notes == ["lesson: report acceptance only", "evidence: e1 (success) — ok"]


async def test_stale_expected_version_on_update_body_conflicts_and_writes_nothing(heart, session):
    detail = await heart.store_procedure(_input(), session=session)
    await heart.update_procedure_body(detail.id, _input(description="v2"), session=session)

    with pytest.raises(ProcedureConflictError):
        await heart.update_procedure_body(detail.id, _input(description="v3"), session=session, expected_version=1)

    current = await heart.get_procedure(detail.id, session=session)
    assert current.version == 2
    assert current.description == "v2"
    # The current version is accepted.
    third = await heart.update_procedure_body(detail.id, _input(description="v3"), session=session, expected_version=2)
    assert third.version == 3


async def test_stale_expected_version_on_append_notes_conflicts(heart, session):
    detail = await heart.store_procedure(_input(), session=session)
    await heart.update_procedure_body(detail.id, _input(description="v2"), session=session)

    with pytest.raises(ProcedureConflictError):
        await heart.append_procedure_notes(detail.id, lambda notes: ["x"], session=session, expected_version=1)
    assert (await heart.get_procedure(detail.id, session=session)).implementation_notes == [
        "lesson: report acceptance only"
    ]


async def test_restore_brings_back_an_old_body_as_a_new_version(heart, session):
    detail = await heart.store_procedure(_input(), session=session)
    await heart.update_procedure_body(
        detail.id, _input(description="Bad rewrite", core_patterns=["wrong"]), session=session
    )

    restored = await heart.restore_procedure_version(detail.id, 1, session=session)

    assert restored.version == 3
    assert restored.description == "Say a send was accepted, not delivered"
    assert restored.core_patterns == ["accepted is not delivered"]
    versions = await heart.procedures.list_versions(detail.id, session=session)
    assert [(v.version, v.change_source) for v in versions][-1] == (3, "restore")
    assert versions[-1].reason == "restored version 1"


async def test_restore_of_a_missing_version_raises(heart, session):
    detail = await heart.store_procedure(_input(), session=session)
    with pytest.raises(ValueError, match="version 7"):
        await heart.restore_procedure_version(detail.id, 7, session=session)


async def test_restore_with_stale_expected_version_conflicts(heart, session):
    detail = await heart.store_procedure(_input(), session=session)
    await heart.update_procedure_body(detail.id, _input(description="v2"), session=session)
    with pytest.raises(ProcedureConflictError):
        await heart.restore_procedure_version(detail.id, 1, session=session, expected_version=1)


async def test_restore_refuses_a_name_another_active_procedure_holds(heart, session):
    detail = await heart.store_procedure(_input(name="Old name", tags=["manual"]), session=session)
    await heart.update_procedure_body(detail.id, _input(name="New name", tags=["manual"]), session=session)
    await heart.store_procedure(_input(name="Old name", description="someone else", tags=["manual"]), session=session)

    with pytest.raises(ProcedureConflictError, match="Old name"):
        await heart.restore_procedure_version(detail.id, 1, session=session)
    assert (await heart.get_procedure(detail.id, session=session)).version == 2


# ---------------------------------------------------------------------------
# Create-time dedupe: idempotency key
# ---------------------------------------------------------------------------


async def test_idempotency_key_replay_returns_the_existing_procedure(heart, session):
    first = await heart.store_procedure(_input(idempotency_key="lesson-1"), session=session)
    replay = await heart.store_procedure(_input(idempotency_key="lesson-1"), session=session)

    assert replay.id == first.id
    assert replay.version == 1
    assert await _active_count(session, heart.agent_id) == 1


async def test_idempotency_key_reused_with_a_different_input_conflicts(heart, session):
    await heart.store_procedure(_input(idempotency_key="lesson-1"), session=session)
    with pytest.raises(ProcedureConflictError, match="lesson-1"):
        await heart.store_procedure(
            _input(name="Something else", idempotency_key="lesson-1"), session=session
        )


async def test_idempotency_replay_ignores_the_key_order_of_runtime_metadata(heart, session):
    first = await heart.store_procedure(
        _input(idempotency_key="ordered", runtime_metadata={"a": 1, "b": {"x": 1, "y": 2}}), session=session
    )
    replay = await heart.store_procedure(
        _input(idempotency_key="ordered", runtime_metadata={"b": {"y": 2, "x": 1}, "a": 1}), session=session
    )
    assert replay.id == first.id


async def test_idempotency_replay_works_with_dedupe_off(db, mock_embeddings, session):
    manager = ProcedureManager(db, mock_embeddings, "f102-flag-off", dedupe_enabled=False)
    first = await manager.store(_input(idempotency_key="k"), session=session)
    replay = await manager.store(_input(idempotency_key="k"), session=session)
    assert replay.id == first.id


# ---------------------------------------------------------------------------
# Create-time dedupe: normalized name lock
# ---------------------------------------------------------------------------


async def test_name_lock_merges_a_same_named_learned_procedure(heart, session):
    first = await heart.store_procedure(_input(), session=session)

    merged = await heart.store_procedure(
        _input(
            name="  report SEND   results-honestly ",
            core_patterns=["accepted is not delivered", "quote the tool's status"],
            implementation_notes=["evidence: e9 (success) — said accepted"],
        ),
        session=session,
    )

    assert merged.id == first.id
    assert merged.version == 2
    assert merged.name == "Report send results honestly"
    assert merged.core_patterns == ["accepted is not delivered", "quote the tool's status"]
    assert merged.implementation_notes == [
        "lesson: report acceptance only",
        "evidence: e9 (success) — said accepted",
    ]
    versions = await heart.procedures.list_versions(first.id, session=session)
    assert versions[-1].change_source == "dedupe_merge"
    assert "name" in versions[-1].reason
    assert "report SEND   results-honestly" in versions[-1].reason
    assert await _active_count(session, heart.agent_id) == 1


async def test_name_lock_leaves_skills_to_update_body(heart, session):
    await heart.store_procedure(_input(name="deploy-service", tags=["skill", "local"]), session=session)

    second = await heart.store_procedure(
        _input(name="Deploy  Service", tags=["skill", "local"]), session=session
    )

    assert second.version == 1
    assert await _active_count(session, heart.agent_id) == 2


async def test_name_lock_does_not_merge_into_a_strategy_card(heart, session):
    card = await heart.store_procedure(
        _input(name="Gate merges on green CI", kind="strategy", tags=["ci"]), session=session
    )
    how_to = await heart.store_procedure(_input(name="Gate merges on green CI"), session=session)

    assert how_to.id != card.id
    assert how_to.version == 1


async def test_name_lock_ignores_a_retired_procedure(heart, session):
    old = await heart.store_procedure(_input(), session=session)
    await heart.retire_procedure(old.id, session=session)

    fresh = await heart.store_procedure(_input(), session=session)

    assert fresh.id != old.id


async def test_inactive_input_is_never_merged(heart, session):
    first = await heart.store_procedure(_input(), session=session)
    inactive = await heart.store_procedure(_input(active=False), session=session)
    assert inactive.id != first.id
    assert inactive.active is False


async def test_flag_off_is_the_legacy_insert(db, mock_embeddings, session):
    manager = ProcedureManager(db, mock_embeddings, "f102-flag-off", dedupe_enabled=False)
    first = await manager.store(_input(), session=session)
    second = await manager.store(_input(name="report send results  honestly"), session=session)

    assert second.id != first.id
    assert second.version == 1


# ---------------------------------------------------------------------------
# Create-time dedupe: semantic near-duplicate merge (pgvector)
# ---------------------------------------------------------------------------


async def test_semantic_near_duplicate_of_a_learned_procedure_is_merged(db, session):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.90)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_threshold=0.85)
    first = await manager.store(_input(name="Honest send reports", description="ALPHA"), session=session)

    merged = await manager.store(
        _input(
            name="Report email sends without overclaiming",
            description="BRAVO",
            implementation_notes=["lesson: say accepted, not delivered"],
        ),
        session=session,
    )

    assert merged.id == first.id
    assert merged.name == "Honest send reports"
    assert merged.implementation_notes == ["lesson: report acceptance only", "lesson: say accepted, not delivered"]
    versions = await manager.list_versions(first.id, session=session)
    assert versions[-1].change_source == "dedupe_merge"
    assert "cosine 0.900" in versions[-1].reason
    assert "Report email sends without overclaiming" in versions[-1].reason


async def test_semantic_neighbour_below_the_threshold_is_inserted(db, session):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.80)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_threshold=0.85)
    first = await manager.store(_input(name="Honest send reports", description="ALPHA"), session=session)
    second = await manager.store(_input(name="Report email sends", description="BRAVO"), session=session)
    assert second.id != first.id


async def test_semantic_merge_skips_hand_authored_skills(db, session):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.95)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_threshold=0.85)
    first = await manager.store(
        _input(name="evolution-gate", description="ALPHA", tags=["skill", "local"]), session=session
    )
    second = await manager.store(
        _input(name="algorithm-evolution", description="BRAVO", tags=["skill", "local"]), session=session
    )
    assert second.id != first.id


async def test_semantic_merge_is_off_with_the_flag(db, session):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.95)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_enabled=False)
    first = await manager.store(_input(name="Honest send reports", description="ALPHA"), session=session)
    second = await manager.store(_input(name="Report email sends", description="BRAVO"), session=session)
    assert second.id != first.id


async def test_semantic_merge_needs_a_learned_target(db, session):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.95)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_threshold=0.85)
    hand_written = await manager.store(_input(name="Honest send reports", description="ALPHA", tags=["manual"]),
                                       session=session)
    learned = await manager.store(_input(name="Report email sends", description="BRAVO"), session=session)
    assert learned.id != hand_written.id


async def test_semantic_merge_needs_a_learned_input(db, session):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.95)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_threshold=0.85)
    learned = await manager.store(_input(name="Honest send reports", description="ALPHA"), session=session)
    hand_written = await manager.store(_input(name="Report email sends", description="BRAVO", tags=["manual"]),
                                       session=session)
    assert hand_written.id != learned.id


@pytest.mark.parametrize("kind", ["strategy", "job_lesson"])
async def test_semantic_merge_never_targets_a_card_or_a_job_lesson(db, session, kind):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.95)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_threshold=0.85)
    other = await manager.store(
        _input(name="A lesson of another kind", description="ALPHA", kind=kind,
               runtime_metadata={"job": {"schedule_id": "s1"}} if kind == "job_lesson" else None),
        session=session,
    )
    learned = await manager.store(_input(name="Report email sends", description="BRAVO"), session=session)
    assert learned.id != other.id


async def test_a_merge_keeps_the_stats_and_re_embeds(db, session):
    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.90)})
    manager = ProcedureManager(db, embeddings, "f102-semantic", dedupe_threshold=0.85)
    first = await manager.store(
        _input(name="Honest send reports", description="ALPHA", runtime_metadata={"skip_until_scored": 10}),
        session=session,
    )
    row = await session.get(Procedure, first.id)
    row.success_count, row.activation_count = 7, 9
    row.embedding = [2.0] + [0.0] * 1535  # same direction as ALPHA, other length: a merge re-embeds it
    await session.flush()

    merged = await manager.store(_input(name="Report email sends", description="BRAVO",
                                        core_patterns=["a new pattern"]), session=session)

    assert merged.id == first.id
    await session.refresh(row)
    assert (row.success_count, row.activation_count) == (7, 9)
    assert row.runtime_metadata == {"skip_until_scored": 10}
    assert list(row.embedding)[0] == pytest.approx(1.0)


async def test_a_keyed_request_that_merged_replays_to_the_merged_row(heart, session):
    first = await heart.store_procedure(_input(), session=session)
    merged = await heart.store_procedure(_input(name="report send results honestly", idempotency_key="k1"),
                                         session=session)
    assert (merged.id, merged.version) == (first.id, 2)

    replay = await heart.store_procedure(_input(name="report send results honestly", idempotency_key="k1"),
                                         session=session)

    assert (replay.id, replay.version) == (first.id, 2)
    assert len(await heart.procedures.list_versions(first.id, session=session)) == 2
    with pytest.raises(ProcedureConflictError):
        await heart.store_procedure(_input(name="report send results honestly", description="other",
                                           idempotency_key="k1"), session=session)


async def test_an_empty_idempotency_key_is_refused():
    with pytest.raises(ValueError):
        _input(idempotency_key="")


async def test_another_agent_neither_replays_nor_merges_nor_reads_history(db, mock_embeddings, session):
    a = ProcedureManager(db, mock_embeddings, "f102-agent-a")
    b = ProcedureManager(db, mock_embeddings, "f102-agent-b")
    first = await a.store(_input(idempotency_key="shared"), session=session)

    other = await b.store(_input(idempotency_key="shared"), session=session)

    assert other.id != first.id
    assert other.version == 1
    assert await b.list_versions(first.id, session=session) == []
    with pytest.raises(ValueError, match="not found"):
        await b.restore(first.id, 1, session=session)


async def test_the_heart_passes_the_dedupe_flag_on(db, mock_embeddings, session):
    from nous.config import Settings
    from nous.heart import Heart

    heart = Heart(db, Settings(procedure_dedupe_enabled=False), embedding_provider=mock_embeddings)
    try:
        first = await heart.store_procedure(_input(), session=session)
        second = await heart.store_procedure(_input(name="report send results  honestly"), session=session)
    finally:
        await heart.close()
    assert second.id != first.id


async def test_the_stored_event_after_a_merge_names_the_surviving_row(heart, session):
    events = []

    class _Bus:
        async def emit(self, event):
            events.append(event)

    heart._bus = _Bus()
    first = await heart.store_procedure(_input(), session=session)
    await heart.store_procedure(_input(name="report send results  honestly", description="incoming"),
                                session=session)

    assert [e.data["procedure_id"] for e in events] == [str(first.id), str(first.id)]
    assert events[-1].data["name"] == "Report send results honestly"
    assert events[-1].data["description"] == "Say a send was accepted, not delivered"


async def test_writers_that_do_not_change_the_body_keep_the_version(heart, session):
    detail = await heart.store_procedure(_input(), session=session)
    await heart.activate_procedure(detail.id, session=session)
    await heart.record_procedure_outcome(detail.id, "success", session=session)
    await heart.retire_procedure(detail.id, session=session)
    await heart.procedures.reactivate(detail.id, session=session)

    assert (await heart.get_procedure(detail.id, session=session)).version == 1
    assert len(await heart.procedures.list_versions(detail.id, session=session)) == 1


async def test_the_name_lock_never_merges_into_a_skill(heart, session):
    skill = await heart.store_procedure(_input(name="deploy-service", tags=["skill", "local"]), session=session)
    learned = await heart.store_procedure(_input(name="Deploy Service"), session=session)
    lesson_named_like_it = await heart.store_procedure(_input(name="deploy_service", tags=["auto:x"]),
                                                       session=session)
    assert learned.id != skill.id
    assert lesson_named_like_it.id != skill.id


async def test_a_stale_conflict_writes_no_version(heart, session):
    detail = await heart.store_procedure(_input(), session=session)
    with pytest.raises(ProcedureConflictError):
        await heart.update_procedure_body(detail.id, _input(description="x"), session=session, expected_version=5)
    assert [v.version for v in await _versions(session, detail.id)] == [1]


async def test_restore_takes_a_name_back_from_a_strategy_card(heart, session):
    detail = await heart.store_procedure(_input(name="Old name", tags=["manual"]), session=session)
    await heart.update_procedure_body(detail.id, _input(name="New name", tags=["manual"]), session=session)
    card = await heart.store_procedure(_input(name="Old name", kind="strategy", tags=["ci"]), session=session)

    restored = await heart.restore_procedure_version(detail.id, 1, session=session)

    assert restored.name == "Old name"
    assert restored.tags == ["manual"]
    assert restored.implementation_notes == ["lesson: report acceptance only"]
    assert (await heart.get_procedure(card.id, session=session)).name != "Old name"


async def test_restore_of_an_unknown_procedure_raises(heart, session):
    from uuid import uuid4

    with pytest.raises(ValueError, match="not found"):
        await heart.restore_procedure_version(uuid4(), 1, session=session)


async def test_two_concurrent_stores_of_one_lesson_leave_one_row(db, mock_embeddings):
    """Committed (no SAVEPOINT): the advisory lock orders the two creates, so the second merges."""
    import asyncio

    from sqlalchemy import delete

    agent = "f102-concurrent"
    manager = ProcedureManager(db, mock_embeddings, agent)
    try:
        await asyncio.gather(
            manager.store(_input()),
            manager.store(_input(name="report send results honestly", implementation_notes=["evidence: e2"])),
        )
        async with db.session() as check:
            rows = (await check.execute(
                select(Procedure).where(Procedure.agent_id == agent).where(Procedure.active == True)  # noqa: E712
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].version == 2
            assert [v.change_source for v in await manager.list_versions(rows[0].id, session=check)] == [
                "create", "dedupe_merge"
            ]
    finally:
        async with db.session() as cleanup:
            await cleanup.execute(delete(Procedure).where(Procedure.agent_id == agent))
            await cleanup.commit()


async def test_a_hand_written_input_is_never_merged(heart, session):
    """MCP teach stores name + description only: a same-named second teach is its own row."""
    learned = await heart.store_procedure(_input(), session=session)
    taught = await heart.store_procedure(
        ProcedureInput(name="report send results-honestly", domain=_DOMAIN, description="other content"),
        session=session,
    )
    assert taught.id != learned.id
    assert taught.description == "other content"


async def test_a_learned_input_never_merges_into_a_hand_written_row(heart, session):
    hand_written = await heart.store_procedure(_input(tags=["manual"]), session=session)
    learned = await heart.store_procedure(_input(name="report send results-honestly"), session=session)
    assert learned.id != hand_written.id
    assert (await heart.get_procedure(hand_written.id, session=session)).tags == ["manual"]


async def test_a_merge_that_adds_nothing_writes_no_version(heart, session):
    first = await heart.store_procedure(_input(), session=session)
    again = await heart.store_procedure(_input(name="report send results honestly"), session=session)
    assert (again.id, again.version) == (first.id, 1)
    assert len(await heart.procedures.list_versions(first.id, session=session)) == 1


async def test_a_merge_records_the_incoming_description(heart, session):
    first = await heart.store_procedure(_input(), session=session)
    await heart.store_procedure(
        _input(name="report send results honestly", description="Incoming wording", core_patterns=["new"]),
        session=session,
    )
    versions = await heart.procedures.list_versions(first.id, session=session)
    assert "Incoming wording" in versions[-1].reason


# ---------------------------------------------------------------------------
# The learner's weak review: "revise" rewrites the row in place
# ---------------------------------------------------------------------------


async def _weak_review_setup(db, agent: str):
    """A committed heart for ``agent`` with siblings A and B at cosine 0.90; A is weak (0/5)."""
    from nous.config import Settings
    from nous.heart import Heart

    embeddings = _AxisEmbeddings({"ALPHA": 0.0, "BRAVO": _angle_for(0.90), "REVISED": _angle_for(0.95)})
    heart = Heart(db, Settings(agent_id=agent), embedding_provider=embeddings)
    # Stored without dedupe: two learned siblings close enough for a create-time merge.
    setup = ProcedureManager(db, embeddings, agent, dedupe_enabled=False)
    a = await setup.store(_input(name="Sibling A", description="ALPHA", core_patterns=["a pattern"]))
    b = await setup.store(_input(name="Sibling B", description="BRAVO", core_patterns=["b pattern"]))
    async with db.session() as s:
        row = await s.get(Procedure, a.id)
        row.success_count, row.failure_count, row.activation_count = 0, 5, 5
        await s.commit()
    return heart, a, b


def _weak_review_learner(heart, answer: dict):
    from unittest.mock import AsyncMock

    from nous.handlers.procedure_learner import ProcedureLearner

    learner = ProcedureLearner(AsyncMock(), heart, None, heart.settings, AsyncMock())
    learner._call_llm = AsyncMock(return_value=answer)
    return learner


async def _cleanup(db, agent: str) -> None:
    from sqlalchemy import delete

    async with db.session() as cleanup:
        await cleanup.execute(delete(Procedure).where(Procedure.agent_id == agent))
        await cleanup.commit()


_REVISE = {
    "action": "revise",
    "reason": "too vague",
    "revised_description": "REVISED: say accepted, never delivered",
    "revised_patterns": ["quote the tool's status"],
}


async def test_a_weak_review_revise_rewrites_the_row_and_leaves_its_sibling_alone(db):
    agent = "f102-weak-revise"
    heart, a, b = await _weak_review_setup(db, agent)
    try:
        learner = _weak_review_learner(heart, _REVISE)
        heart.search_procedures = _returns([a])

        assert await learner._review_weak_procedures() == 1

        revised = await heart.get_procedure(a.id)
        assert revised.active is True
        assert revised.description == "REVISED: say accepted, never delivered"
        assert revised.core_patterns == ["quote the tool's status"]
        assert revised.version == 2
        assert (revised.success_count, revised.failure_count) == (0, 5)
        sibling = await heart.get_procedure(b.id)
        assert (sibling.version, sibling.description, sibling.core_patterns) == (1, "BRAVO", ["b pattern"])
        versions = await heart.list_procedure_versions(a.id)
        assert [v.change_source for v in versions] == ["create", "update_body"]
        assert "too vague" in versions[-1].reason
    finally:
        await _cleanup(db, agent)
        await heart.close()


async def test_the_next_weak_review_leaves_a_freshly_revised_row_alone(db):
    agent = "f102-weak-revise-grace"
    heart, a, _b = await _weak_review_setup(db, agent)
    try:
        learner = _weak_review_learner(heart, _REVISE)
        heart.search_procedures = _returns([a])
        await learner._review_weak_procedures()
        learner._call_llm.reset_mock()

        # Still 0/5, so weak on its old score: the revision has not been tried yet.
        assert await learner._review_weak_procedures() == 0
        learner._call_llm.assert_not_called()
        assert (await heart.get_procedure(a.id)).version == 2
    finally:
        await _cleanup(db, agent)
        await heart.close()


async def test_a_weak_review_revise_keeps_the_rows_runtime_metadata(db):
    agent = "f102-weak-revise-metadata"
    heart, a, _b = await _weak_review_setup(db, agent)
    try:
        async with db.session() as s:
            (await s.get(Procedure, a.id)).runtime_metadata = {"skip_until_scored": 10}
            await s.commit()
        learner = _weak_review_learner(heart, _REVISE)
        heart.search_procedures = _returns([a])

        await learner._review_weak_procedures()

        async with db.session() as s:
            assert (await s.get(Procedure, a.id)).runtime_metadata == {"skip_until_scored": 10}
    finally:
        await _cleanup(db, agent)
        await heart.close()


def _returns(value):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)
