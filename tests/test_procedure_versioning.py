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
