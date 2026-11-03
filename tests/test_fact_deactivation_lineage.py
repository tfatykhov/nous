"""Migration 094: every fact deactivation records why on the row, and the stale
scan keeps person and preference facts.

The tier-1 profile channel renders person/preference/rule facts every turn
without stamping ``last_recalled_at``, so the stale scan used to deactivate
still-true user facts. ``deactivated_at`` / ``deactivation_reason`` outlive the
30-day ``consolidation_actions`` retention.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from nous.config import Settings
from nous.heart.schemas import FactInput
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import Fact

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "094_fact_deactivation_reason.sql"
REASONS = {"stale", "superseded", "merged", "removed", "user", "rollback"}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def _reasons(check_sql: str) -> set[str]:
    inside = re.search(r"deactivation_reason IN \(([^)]*)\)", check_sql)
    assert inside is not None, check_sql
    return set(re.findall(r"'([a-z_]+)'", inside.group(1)))


# ---------------------------------------------------------------------------
# The migration file
# ---------------------------------------------------------------------------


def test_the_migration_adds_both_columns_and_a_named_check():
    statements = _split_sql_statements(_sql())
    assert len(statements) == 4
    assert all(s.startswith("ALTER TABLE heart.facts") for s in statements)
    assert "ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ" in statements[0]
    assert "ADD COLUMN IF NOT EXISTS deactivation_reason TEXT" in statements[1]
    assert "DROP CONSTRAINT IF EXISTS chk_facts_deactivation_reason" in statements[2]
    assert "ADD CONSTRAINT chk_facts_deactivation_reason" in statements[3]
    assert "deactivation_reason IS NULL" in statements[3]


def test_the_migration_has_nothing_the_splitter_would_mangle_and_no_grants():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())  # text() reads :name as a bind parameter
    assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))
    code = _code().upper()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code), word


def test_the_reason_list_matches_the_orm_constraint_and_the_type():
    from nous.heart.facts import FactDeactivationReason

    statement = _split_sql_statements(_sql())[3]
    (constraint,) = [c for c in Fact.__table__.constraints if c.name == "chk_facts_deactivation_reason"]
    assert _reasons(statement) == _reasons(str(constraint.sqltext)) == set(get_args(FactDeactivationReason)) == REASONS


def test_both_columns_are_nullable_on_the_orm_model():
    assert Fact.__table__.c.deactivated_at.nullable
    assert Fact.__table__.c.deactivation_reason.nullable


def test_the_eval_preflight_probes_both_columns():
    from nous_eval.schema_preflight import migration_artifacts

    assert migration_artifacts(_sql()) == [
        ("column", "heart", "facts", "deactivated_at"),
        ("column", "heart", "facts", "deactivation_reason"),
    ]


@pytest.mark.postgres_only
async def test_the_migration_applies_twice(session):
    # DDL is transactional in Postgres: the session fixture rolls all of it back.
    for _ in range(2):
        for statement in _split_sql_statements(_sql()):
            await session.execute(text(statement))
    rows = (
        await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'heart' AND table_name = 'facts' "
                "AND column_name IN ('deactivated_at', 'deactivation_reason') ORDER BY column_name"
            )
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("deactivated_at", "timestamp with time zone", "YES"),
        ("deactivation_reason", "text", "YES"),
    ]
    n_checks = (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'heart.facts'::regclass AND conname = 'chk_facts_deactivation_reason'"
            )
        )
    ).scalar_one()
    assert n_checks == 1


@pytest.mark.postgres_only
async def test_the_check_refuses_an_unknown_reason(heart, session):
    from sqlalchemy.exc import IntegrityError

    fact = await heart.learn(FactInput(content="The lineage check refuses a reason it does not know."), session=session)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE heart.facts SET deactivation_reason = 'forgotten' WHERE id = :id"), {"id": fact.id}
            )


# ---------------------------------------------------------------------------
# The stale-scan policy (G-1/K-1)
# ---------------------------------------------------------------------------


def test_the_default_exclusion_covers_the_profile_categories():
    default = Settings.model_fields["stale_scan_excluded_categories"].default_factory()
    assert set(default) == {"rule", "person", "preference"}


@pytest.mark.postgres_only
async def test_the_stale_scan_keeps_person_and_preference_facts_and_stamps_stale(heart):
    from nous.handlers.sleep_handler import SleepHandler

    agent_id = f"test-094-stale-{uuid4()}"
    orig_heart_agent, orig_facts_agent = heart.agent_id, heart.facts.agent_id
    orig_excluded = heart.settings.stale_scan_excluded_categories
    heart.agent_id = agent_id
    heart.facts.agent_id = agent_id
    # The shipped default, not whatever the environment sets.
    heart.settings.stale_scan_excluded_categories = Settings.model_fields[
        "stale_scan_excluded_categories"
    ].default_factory()
    aged = datetime.now(UTC) - timedelta(days=heart.settings.stale_scan_age_days + 30)
    ids = {category: uuid4() for category in ("person", "preference", "rule", "technical")}
    try:
        async with heart.db.session() as s:
            for category, fid in ids.items():
                s.add(
                    Fact(
                        id=fid,
                        agent_id=agent_id,
                        subject="stale-scan lineage",
                        content=f"An aged {category} fact that search never recalled.",
                        category=category,
                        active=True,
                        created_at=aged,
                        last_recalled_at=None,
                    )
                )
            await s.commit()

        handler = SleepHandler(MagicMock(), heart, heart.settings, MagicMock(), llm_client=None)
        stats: dict = {}
        before = datetime.now(UTC)
        assert await handler._phase_stale_scan(stats) is True
        assert stats["stale_deactivated"] == 1

        async with heart.db.session() as s:
            for category in ("person", "preference", "rule"):
                kept = await s.get(Fact, ids[category])
                assert kept.active is True, f"the stale scan deactivated a {category} fact"
                assert kept.deactivated_at is None and kept.deactivation_reason is None
            gone = await s.get(Fact, ids["technical"])
            assert gone.active is False
            assert gone.deactivation_reason == "stale"
            assert gone.deactivated_at is not None and gone.deactivated_at >= before - timedelta(seconds=5)
    finally:
        heart.agent_id = orig_heart_agent
        heart.facts.agent_id = orig_facts_agent
        heart.settings.stale_scan_excluded_categories = orig_excluded
        async with heart.db.session() as s:
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent_id})
            await s.commit()


# ---------------------------------------------------------------------------
# The three primitives (K-5)
# ---------------------------------------------------------------------------


async def _row(session, fact_id) -> Fact:
    row = await session.get(Fact, fact_id)
    await session.refresh(row)
    return row


@pytest.mark.postgres_only
async def test_apply_supersession_stamps_superseded(heart, session):
    winner = await heart.learn(
        FactInput(content="The release train now leaves every Thursday at noon."), session=session
    )
    loser = await heart.learn(
        FactInput(content="Ledger rows are archived to cold storage after ninety days."), session=session
    )
    assert await heart.facts.apply_supersession(winner.id, loser.id, session) is True
    await session.flush()
    loser_row = await _row(session, loser.id)
    assert loser_row.active is False
    assert loser_row.deactivation_reason == "superseded"
    assert loser_row.deactivated_at is not None
    winner_row = await _row(session, winner.id)
    assert winner_row.deactivated_at is None and winner_row.deactivation_reason is None


@pytest.mark.postgres_only
async def test_supersede_stamps_superseded_by_default(heart, session):
    old = await heart.learn(FactInput(content="The staging cluster runs Postgres sixteen."), session=session)
    await heart.supersede_fact(
        old.id, FactInput(content="The staging cluster runs Postgres seventeen."), session=session
    )
    row = await _row(session, old.id)
    assert row.active is False
    assert row.deactivation_reason == "superseded"
    assert row.deactivated_at is not None


@pytest.mark.postgres_only
async def test_supersede_stamps_the_reason_it_is_given(heart, session):
    old = await heart.learn(FactInput(content="Weekly planning happens on Monday mornings."), session=session)
    await heart.supersede_fact(
        old.id, FactInput(content="Weekly planning happens on Tuesday mornings."), session=session, reason="user"
    )
    row = await _row(session, old.id)
    assert row.active is False
    assert row.deactivation_reason == "user"


@pytest.mark.postgres_only
async def test_deactivate_stamps_removed_by_default(heart, session):
    fact = await heart.learn(FactInput(content="The nightly export writes to the reports bucket."), session=session)
    await heart.deactivate_fact(fact.id, session=session)
    row = await _row(session, fact.id)
    assert row.active is False
    assert row.deactivation_reason == "removed"
    assert row.deactivated_at is not None


@pytest.mark.postgres_only
async def test_deactivate_stamps_the_reason_it_is_given(heart, session):
    fact = await heart.learn(FactInput(content="The on-call rotation hands over on Fridays."), session=session)
    await heart.deactivate_fact(fact.id, session=session, reason="user")
    row = await _row(session, fact.id)
    assert row.active is False
    assert row.deactivation_reason == "user"


@pytest.mark.postgres_only
async def test_an_inactive_fact_keeps_the_stamp_it_went_inactive_with(heart, session):
    fact = await heart.learn(FactInput(content="The cache warms itself from the replica on boot."), session=session)
    await heart.deactivate_fact(fact.id, session=session, reason="user")
    first = await _row(session, fact.id)
    stamped_at = first.deactivated_at
    await heart.deactivate_fact(fact.id, session=session)
    row = await _row(session, fact.id)
    assert row.deactivation_reason == "user"
    assert row.deactivated_at == stamped_at


@pytest.mark.postgres_only
async def test_breaking_a_supersession_cycle_reactivates_and_clears_the_stamp(heart, session):
    a = await heart.learn(FactInput(content="Fact A: the quarterly review is in the first week."), session=session)
    b = await heart.learn(FactInput(content="Fact B: the quarterly review moved to the second week."), session=session)
    a_row = await session.get(Fact, a.id)
    b_row = await session.get(Fact, b.id)
    b_row.learned_at = a_row.learned_at + timedelta(seconds=10)
    a_row.superseded_by = b.id
    b_row.superseded_by = a.id
    b_row.active = False
    b_row.deactivated_at = datetime.now(UTC)
    b_row.deactivation_reason = "superseded"
    await session.flush()

    result = await heart.facts._get_current(a.id, session)

    assert result.id == b.id
    await session.refresh(b_row)
    assert b_row.active is True
    assert b_row.superseded_by is None
    assert b_row.deactivated_at is None
    assert b_row.deactivation_reason is None


# ---------------------------------------------------------------------------
# The exemplar backfill rollback (raw SQL)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_exemplar_rollbacks_stamp_rollback_and_keep_an_earlier_stamp(heart):
    from scripts.backfill_exemplar_facts import rollback_exemplar_facts, rollback_exemplar_facts_by_manifest

    agent_id = f"test-094-rollback-{uuid4()}"
    by_watermark, by_manifest, already_gone = uuid4(), uuid4(), uuid4()
    async with heart.db.session() as s:
        watermark = (await s.execute(text("SELECT now()"))).scalar_one()
        earlier = watermark - timedelta(days=1)
        for i, fid in enumerate((by_watermark, by_manifest, already_gone)):
            s.add(
                Fact(
                    id=fid,
                    agent_id=agent_id,
                    content=f"exemplar {i}\nlabel: {i}",
                    source="exemplar_extractor",
                    active=fid != already_gone,
                    deactivated_at=earlier if fid == already_gone else None,
                    deactivation_reason="superseded" if fid == already_gone else None,
                    created_at=watermark + timedelta(seconds=1 + i),
                )
            )
        await s.commit()
    try:
        async with heart.db.session() as s:
            await rollback_exemplar_facts_by_manifest(s, agent_id=agent_id, fact_ids=[by_manifest], dry_run=False)
            await s.commit()
        async with heart.db.session() as s:
            await rollback_exemplar_facts(s, agent_id=agent_id, watermark=watermark, dry_run=False)
            await s.commit()
        async with heart.db.session() as s:
            for fid in (by_watermark, by_manifest):
                row = await s.get(Fact, fid)
                assert row.active is False
                assert row.deactivation_reason == "rollback"
                assert row.deactivated_at is not None
            kept = await s.get(Fact, already_gone)
            assert kept.active is False
            assert kept.deactivation_reason == "superseded"
            assert kept.deactivated_at == earlier
    finally:
        async with heart.db.session() as s:
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent_id})
            await s.commit()
