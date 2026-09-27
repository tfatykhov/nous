"""Baked eval-DB images carry a schema but no migration history.

Dockerfile.eval-db runs init.sql + every migration through initdb, which
never writes nous_system.schema_migrations. The eval harness then calls
run_migrations, which re-runs everything and dies on the first
non-idempotent statement (022's bare CREATE UNIQUE INDEX). These tests build
exactly that DB shape on a live Postgres — every migration applied raw, then
073 rolled back to mimic an image baked before it — and check that seeding
the history lets run_migrations apply 073 alone.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from nous.storage.migrator import _MIGRATIONS_DIR, run_migrations
from nous_eval.schema_preflight import migration_artifacts, seed_baked_migration_history

_INIT_SQL = Path(__file__).resolve().parent.parent / "sql" / "init.sql"
_M073 = "073_decision_calibration_provenance"


def test_migration_artifacts_parses_tables_indexes_and_columns() -> None:
    m022 = migration_artifacts((_MIGRATIONS_DIR / "022_rubric_outcome_signals.sql").read_text())
    assert ("table", "heart", "rubric_versions") in m022
    assert ("index", "idx_rubric_active_agent") in m022

    m073 = migration_artifacts((_MIGRATIONS_DIR / f"{_M073}.sql").read_text())
    assert m073 == [
        ("column", "brain", "decisions", "calibration_factor"),
        ("column", "brain", "decisions", "calibration_applied_at"),
        ("index", "idx_decisions_calibration_applied_at"),
    ]


def _url(dbname: str) -> str:
    return (
        f"postgresql+asyncpg://{os.environ.get('DB_USER', 'nous')}:"
        f"{os.environ.get('DB_PASSWORD', 'nous_dev_password')}@"
        f"{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}/{dbname}"
    )


@pytest.fixture
async def baked_pre_073_engine():
    """A scratch DB shaped like nous-eval-db built before migration 073."""
    name = f"nous_bake_test_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(_url(os.environ.get("DB_NAME", "nous")), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(_url(name))
    try:
        # What docker-entrypoint-initdb.d does: run each whole file (simple
        # query protocol, like psql, so init.sql's $$ bodies survive) and
        # record nothing.
        async with engine.begin() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            for path in [_INIT_SQL, *sorted(_MIGRATIONS_DIR.glob("*.sql"))]:
                await raw.execute(path.read_text(encoding="utf-8"))
            await conn.execute(text("DROP INDEX brain.idx_decisions_calibration_applied_at"))
            await conn.execute(
                text("ALTER TABLE brain.decisions DROP COLUMN calibration_factor, DROP COLUMN calibration_applied_at")
            )
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.integration
async def test_run_migrations_alone_fails_on_baked_image(baked_pre_073_engine) -> None:
    """The reported failure: empty history makes the migrator replay 001+.

    Which statement breaks first depends on the init.sql the image was baked
    with (022's duplicate index on v2026-Q2); any replay failure is the bug.
    """
    with pytest.raises(DBAPIError):
        await run_migrations(baked_pre_073_engine)


@pytest.mark.integration
async def test_seeded_history_applies_only_missing_migration(baked_pre_073_engine) -> None:
    seeded = await seed_baked_migration_history(baked_pre_073_engine)
    assert seeded, "baked image must be detected"
    assert _M073 not in seeded
    assert seeded[-1] < _M073

    applied = await run_migrations(baked_pre_073_engine)
    assert applied == [_M073]

    async with baked_pre_073_engine.connect() as conn:
        cols = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'brain' AND table_name = 'decisions'"
                )
            )
        }
    assert {"calibration_factor", "calibration_applied_at"} <= cols

    # Idempotent: history now exists, so a second harness start seeds nothing.
    assert await seed_baked_migration_history(baked_pre_073_engine) == []
    assert await run_migrations(baked_pre_073_engine) == []
