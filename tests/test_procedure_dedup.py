"""Procedure dedup Phase 0 — supersession bookkeeping + restart-loop close.

Covers migration 057 columns and the B1 resurrection-loop fix (audit
docs/reviews/procedure-subsystem-audit-2026-06-06.md): a consolidated duplicate
(archived with superseded_by set) must NOT be recreated by bootstrap or
un-archived by reactivate.

All tests require real Postgres (array columns + the new columns). The first three
use the rollback-isolated ``session`` fixture; the bootstrap/reactivate tests commit
uniquely-named rows (those code paths open their own sessions) and clean up after.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from nous.heart import ProcedureInput
from nous.skills.bootstrap import bootstrap_local_skills, reactivate_skills

pytestmark = pytest.mark.postgres_only


def _inp(name: str, **ov) -> ProcedureInput:
    d = dict(
        name=name,
        domain="general",
        description=f"desc for {name}",
        core_patterns=["p"],
        implementation_notes=["n"],
        tags=["skill", "local"],
    )
    d.update(ov)
    return ProcedureInput(**d)


async def _supersede(heart, *, sibling_id, canonical_id) -> None:
    """Archive sibling_id as superseded by canonical_id (committed, own session)."""
    async with heart.db.session() as s:
        await s.execute(
            text(
                "UPDATE heart.procedures SET active=false, archived_at=now(), superseded_by=:c "
                "WHERE id=:i"
            ),
            {"c": canonical_id, "i": sibling_id},
        )
        await s.commit()


async def _delete(heart, *names) -> None:
    async with heart.db.session() as s:
        await s.execute(
            text("DELETE FROM heart.procedures WHERE agent_id=:a AND name = ANY(:names)"),
            {"a": heart.agent_id, "names": list(names)},
        )
        await s.commit()


# ---------------------------------------------------------------------------
# 1. migration columns round-trip
# ---------------------------------------------------------------------------


async def test_supersession_columns_roundtrip(heart, session):
    canon = await heart.store_procedure(_inp("dedup-canon"), session=session)
    sib = await heart.store_procedure(_inp("dedup-sib"), session=session)
    await session.execute(
        text(
            "UPDATE heart.procedures SET active=false, archived_at=now(), superseded_by=:c WHERE id=:i"
        ),
        {"c": canon.id, "i": sib.id},
    )
    row = (
        await session.execute(
            text("SELECT active, superseded_by::text AS sb, archived_at FROM heart.procedures WHERE id=:i"),
            {"i": sib.id},
        )
    ).mappings().first()
    assert row["active"] is False
    assert row["sb"] == str(canon.id)
    assert row["archived_at"] is not None


# ---------------------------------------------------------------------------
# 2. is_name_superseded
# ---------------------------------------------------------------------------


async def test_is_name_superseded(heart, session):
    canon = await heart.store_procedure(_inp("dedup-keep"), session=session)
    sib = await heart.store_procedure(_inp("dedup-dup"), session=session)
    assert await heart.is_procedure_name_superseded("dedup-dup", session=session) is False
    await session.execute(
        text("UPDATE heart.procedures SET active=false, superseded_by=:c WHERE id=:i"),
        {"c": canon.id, "i": sib.id},
    )
    assert await heart.is_procedure_name_superseded("dedup-dup", session=session) is True
    # a name that was never seen is not superseded
    assert await heart.is_procedure_name_superseded("never-existed-xyz", session=session) is False


# ---------------------------------------------------------------------------
# 3. reactivation list excludes superseded rows
# ---------------------------------------------------------------------------


async def test_list_inactive_skills_excludes_superseded(heart, session):
    canon = await heart.store_procedure(_inp("dedup-canon3"), session=session)
    plain = await heart.store_procedure(_inp("dedup-plain-inactive", active=False), session=session)
    sup = await heart.store_procedure(_inp("dedup-superseded-inactive", active=False), session=session)
    await session.execute(
        text("UPDATE heart.procedures SET superseded_by=:c WHERE id=:i"),
        {"c": canon.id, "i": sup.id},
    )
    names = {p.name for p in await heart.procedures.list_inactive_skills(session=session)}
    assert "dedup-plain-inactive" in names           # ordinary inactive skill: eligible
    assert "dedup-superseded-inactive" not in names  # superseded: never resurrected
    assert plain.id is not None and sup.id is not None


# ---------------------------------------------------------------------------
# 4. bootstrap does not recreate a superseded skill (the B1 loop)
# ---------------------------------------------------------------------------


async def test_bootstrap_skips_superseded_name(heart, tmp_path):
    name = f"Dedup Bootstrap Skill {uuid.uuid4().hex[:8]}"
    try:
        canon = await heart.store_procedure(_inp("dedup-boot-canon"))
        sib = await heart.store_procedure(_inp(name))
        await _supersede(heart, sibling_id=sib.id, canonical_id=canon.id)

        # a SKILL.md on disk with the superseded name
        skill_dir = tmp_path / "skills" / "dedup-boot"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: should not be re-imported\n---\nbody\n",
            encoding="utf-8",
        )

        registered = await bootstrap_local_skills(str(tmp_path), heart)
        assert registered == 0  # skipped, not re-imported

        # and there is still no ACTIVE row with that name
        async with heart.db.session() as s:
            n_active = (
                await s.execute(
                    text("SELECT count(*) FROM heart.procedures WHERE agent_id=:a AND name=:n AND active"),
                    {"a": heart.agent_id, "n": name},
                )
            ).scalar()
        assert n_active == 0
    finally:
        await _delete(heart, name, "dedup-boot-canon")


# ---------------------------------------------------------------------------
# 5. reactivate does not un-archive a superseded skill
# ---------------------------------------------------------------------------


async def test_reactivate_skips_superseded(heart, monkeypatch):
    var = f"DEDUP_REQ_{uuid.uuid4().hex[:8].upper()}"
    monkeypatch.setenv(var, "1")
    sup_name = f"dedup-react-superseded-{uuid.uuid4().hex[:6]}"
    ctl_name = f"dedup-react-control-{uuid.uuid4().hex[:6]}"
    try:
        canon = await heart.store_procedure(_inp("dedup-react-canon"))
        sup = await heart.store_procedure(
            _inp(sup_name, active=False, core_concepts=[f"requires:{var}"])
        )
        ctl = await heart.store_procedure(
            _inp(ctl_name, active=False, core_concepts=[f"requires:{var}"])
        )
        await _supersede(heart, sibling_id=sup.id, canonical_id=canon.id)

        await reactivate_skills(heart)

        async with heart.db.session() as s:
            sup_active = (
                await s.execute(text("SELECT active FROM heart.procedures WHERE id=:i"), {"i": sup.id})
            ).scalar()
            ctl_active = (
                await s.execute(text("SELECT active FROM heart.procedures WHERE id=:i"), {"i": ctl.id})
            ).scalar()
        assert sup_active is False  # superseded: stays archived
        assert ctl_active is True   # control: requires satisfied -> reactivated
    finally:
        await _delete(heart, sup_name, ctl_name, "dedup-react-canon")
