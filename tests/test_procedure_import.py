"""Procedure create/import algorithm fixes (audit §6 PR2).

- update_body refreshes a skill in place, preserving learned stats (bug 9).
- get_procedure_by_name / is_name_superseded match case-insensitively (B6).
- embedding failure retries then stores NULL loudly rather than raising (bug 8).

Requires real Postgres (array columns + pgvector embedding).
"""
from __future__ import annotations

import pytest

from nous.heart import ProcedureInput
from nous.heart.procedures import ProcedureManager

pytestmark = pytest.mark.postgres_only


def _inp(name: str, **ov) -> ProcedureInput:
    d = dict(
        name=name, domain="general", description=f"desc for {name}",
        core_patterns=["p"], implementation_notes=["n"], tags=["skill"],
    )
    d.update(ov)
    return ProcedureInput(**d)


async def test_update_body_preserves_counts(heart, session):
    p = await heart.store_procedure(_inp("import-stats"), session=session)
    await heart.activate_procedure(p.id, session=session)
    await heart.activate_procedure(p.id, session=session)
    await heart.record_procedure_outcome(p.id, "success", session=session)
    await heart.record_procedure_outcome(p.id, "success", session=session)

    updated = await heart.update_procedure_body(
        p.id, _inp("import-stats", description="rewritten body", implementation_notes=["x", "y"]),
        session=session,
    )
    assert updated.description == "rewritten body"
    assert updated.implementation_notes == ["x", "y"]
    # learned stats survive the in-place update (the bug-9 regression)
    assert updated.activation_count == 2
    assert updated.success_count == 2


async def test_get_by_name_case_insensitive(heart, session):
    await heart.store_procedure(_inp("CaseSensitive Skill"), session=session)
    hit = await heart.get_procedure_by_name("casesensitive skill", session=session)
    assert hit is not None
    assert hit.name == "CaseSensitive Skill"


async def test_is_name_superseded_case_insensitive(heart, session):
    from sqlalchemy import text

    canon = await heart.store_procedure(_inp("CI-canon"), session=session)
    sib = await heart.store_procedure(_inp("CI-Dup-Name"), session=session)
    await session.execute(
        text("UPDATE heart.procedures SET active=false, superseded_by=:c WHERE id=:i"),
        {"c": canon.id, "i": sib.id},
    )
    assert await heart.is_procedure_name_superseded("ci-dup-name", session=session) is True


async def test_reactivate_skips_name_collision(heart, session):
    # B6/C2: reactivating an inactive row whose lower(name) matches a live row must
    # skip (not flip), else it violates the unique index and aborts reactivate_skills.
    await heart.store_procedure(_inp("ReactClash"), session=session)  # active
    inactive = await heart.store_procedure(_inp("reactclash", active=False), session=session)
    await heart.reactivate_procedure(inactive.id, session=session)
    detail = await heart.get_procedure(inactive.id, session=session)
    assert detail.active is False  # skipped due to active same-name row


async def test_reactivate_succeeds_when_no_collision(heart, session):
    inactive = await heart.store_procedure(_inp("react-unique-name", active=False), session=session)
    await heart.reactivate_procedure(inactive.id, session=session)
    detail = await heart.get_procedure(inactive.id, session=session)
    assert detail.active is True


class _FailingEmbeddings:
    async def embed(self, text: str):
        raise RuntimeError("embedding backend down")

    async def embed_batch(self, texts):
        raise RuntimeError("embedding backend down")

    async def close(self):
        pass


async def test_embed_with_retry_returns_none(db, session):
    mgr = ProcedureManager(db, _FailingEmbeddings(), "test-import-agent")
    result = await mgr._embed_with_retry("anything", attempts=2)
    assert result is None


class _GoodEmbeddings:
    async def embed(self, text: str):
        return [0.1] * 1536

    async def embed_batch(self, texts):
        return [[0.1] * 1536 for _ in texts]

    async def close(self):
        pass


async def test_update_body_nulls_stale_embedding_on_failure(db, session):
    from sqlalchemy import text

    good = ProcedureManager(db, _GoodEmbeddings(), "test-import-agent2")
    detail = await good._store(_inp("embed-update-skill"), session)
    before = (
        await session.execute(
            text("SELECT embedding IS NOT NULL FROM heart.procedures WHERE id=:i"), {"i": detail.id}
        )
    ).scalar()
    assert before is True

    bad = ProcedureManager(db, _FailingEmbeddings(), "test-import-agent2")
    await bad._update_body(detail.id, _inp("embed-update-skill", description="changed"), session)
    after = (
        await session.execute(
            text("SELECT embedding IS NULL FROM heart.procedures WHERE id=:i"), {"i": detail.id}
        )
    ).scalar()
    assert after is True  # stale vector cleared, not kept


async def test_store_survives_embedding_failure(db, session):
    # _store must not raise when embeddings fail; it stores a NULL-embedding row.
    mgr = ProcedureManager(db, _FailingEmbeddings(), "test-import-agent")
    detail = await mgr._store(_inp("embed-fail-skill"), session)
    assert detail.name == "embed-fail-skill"
    from sqlalchemy import text

    emb = (
        await session.execute(
            text("SELECT embedding IS NULL FROM heart.procedures WHERE id=:i"), {"i": detail.id}
        )
    ).scalar()
    assert emb is True
