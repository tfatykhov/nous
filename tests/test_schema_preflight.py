"""Tests for nous_eval.schema_preflight.

The pre-flight asserts the eval DB has every column the ORM expects
before the harness fires its first heart.recall — surfacing schema
drift early instead of letting it cascade into asyncpg
InFailedSQLTransactionError mid-query.

Tests use mocks rather than a live DB so they run deterministically on
any environment.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous_eval.schema_preflight import (
    EvalDBSchemaDriftError,
    _orm_column_names,
    assert_eval_db_schema_matches_orm,
)


def _make_db_with_columns(by_table: dict[tuple[str, str], set[str]]) -> MagicMock:
    """Build a Database mock whose session returns column names per table.

    by_table maps ``(schema, table) -> set of column names`` that
    information_schema.columns will appear to return for that table.
    Tables not in the dict appear empty (i.e., as if the table doesn't
    exist).
    """
    db = MagicMock()
    session_mock = MagicMock()

    async def _execute(_stmt, params):
        key = (params["schema"], params["table"])
        cols = by_table.get(key, set())
        # information_schema.columns returns one row per column; each row
        # is a tuple-like with column_name at position 0.
        rows = [(c,) for c in cols]
        result = MagicMock()
        result.__iter__ = lambda self: iter(rows)
        return result

    session_mock.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _session():
        yield session_mock

    db.session = _session
    return db


async def test_preflight_passes_when_all_orm_columns_present():
    """No raise when every ORM column exists in the eval DB."""
    from nous.storage.models import Censor, Decision, Episode, Fact, Procedure

    by_table = {
        ("heart", "episodes"): _orm_column_names(Episode),
        ("heart", "facts"): _orm_column_names(Fact),
        ("heart", "procedures"): _orm_column_names(Procedure),
        ("heart", "censors"): _orm_column_names(Censor),
        ("brain", "decisions"): _orm_column_names(Decision),
    }
    db = _make_db_with_columns(by_table)
    await assert_eval_db_schema_matches_orm(db)  # must not raise


async def test_preflight_raises_when_one_column_missing():
    """Missing the column today's bug hit (heart.episodes.session_id)
    must raise with that table + column name in the message."""
    from nous.storage.models import Censor, Decision, Episode, Fact, Procedure

    # Drop session_id from the eval DB columns to simulate the
    # migration-040-missing scenario that bit us today.
    episode_cols = _orm_column_names(Episode) - {"session_id"}
    by_table = {
        ("heart", "episodes"): episode_cols,
        ("heart", "facts"): _orm_column_names(Fact),
        ("heart", "procedures"): _orm_column_names(Procedure),
        ("heart", "censors"): _orm_column_names(Censor),
        ("brain", "decisions"): _orm_column_names(Decision),
    }
    db = _make_db_with_columns(by_table)

    with pytest.raises(EvalDBSchemaDriftError) as excinfo:
        await assert_eval_db_schema_matches_orm(db)

    msg = str(excinfo.value)
    assert "heart.episodes" in msg
    assert "session_id" in msg
    # Remediation hint should be present so the operator knows what to do.
    assert "migrations" in msg.lower()


async def test_preflight_reports_all_drift_in_one_error():
    """When multiple tables drift, the error lists all of them at once
    instead of failing on the first and hiding the rest."""
    from nous.storage.models import Censor, Decision, Episode, Fact, Procedure

    by_table = {
        ("heart", "episodes"): _orm_column_names(Episode) - {"session_id"},
        ("heart", "facts"): _orm_column_names(Fact) - {"actionable"},
        ("heart", "procedures"): _orm_column_names(Procedure),
        ("heart", "censors"): _orm_column_names(Censor),
        ("brain", "decisions"): _orm_column_names(Decision) - {"confidence_raw"},
    }
    db = _make_db_with_columns(by_table)

    with pytest.raises(EvalDBSchemaDriftError) as excinfo:
        await assert_eval_db_schema_matches_orm(db)

    msg = str(excinfo.value)
    # All three drifted columns must appear so the operator sees the
    # full picture and can apply all pending migrations in one pass.
    assert "session_id" in msg
    assert "actionable" in msg
    assert "confidence_raw" in msg


async def test_preflight_raises_when_table_missing_entirely():
    """If a required table doesn't exist at all (empty column list),
    every ORM column for it shows as drifted."""
    from nous.storage.models import Censor, Decision, Fact, Procedure

    # Episodes table has zero columns → every ORM-required column drifts.
    by_table = {
        ("heart", "episodes"): set(),
        ("heart", "facts"): _orm_column_names(Fact),
        ("heart", "procedures"): _orm_column_names(Procedure),
        ("heart", "censors"): _orm_column_names(Censor),
        ("brain", "decisions"): _orm_column_names(Decision),
    }
    db = _make_db_with_columns(by_table)

    with pytest.raises(EvalDBSchemaDriftError) as excinfo:
        await assert_eval_db_schema_matches_orm(db)

    msg = str(excinfo.value)
    assert "heart.episodes" in msg
    # A representative column the ORM definitely models on Episode.
    assert "summary" in msg


async def test_preflight_raises_for_migration_073_columns():
    """Preflight must detect the two new brain.decisions columns added by
    migration 073 (calibration_factor, calibration_applied_at) when they
    are absent from a baked eval-DB image.

    This is the exact scenario the Codex finding on PR #640 describes:
    the nous-eval-db:v2026-Q2 image pre-dates migration 073, so evaluating
    against a live volume without first running migrations would raise
    EvalDBSchemaDriftError and abort all retrieval experiments.
    """
    from nous.storage.models import Censor, Decision, Episode, Fact, Procedure

    decision_cols_pre_073 = _orm_column_names(Decision) - {"calibration_factor", "calibration_applied_at"}
    by_table = {
        ("heart", "episodes"): _orm_column_names(Episode),
        ("heart", "facts"): _orm_column_names(Fact),
        ("heart", "procedures"): _orm_column_names(Procedure),
        ("heart", "censors"): _orm_column_names(Censor),
        ("brain", "decisions"): decision_cols_pre_073,
    }
    db = _make_db_with_columns(by_table)

    with pytest.raises(EvalDBSchemaDriftError) as excinfo:
        await assert_eval_db_schema_matches_orm(db)

    msg = str(excinfo.value)
    assert "brain.decisions" in msg
    assert "calibration_factor" in msg or "calibration_applied_at" in msg


async def test_eval_harness_runs_migrations_before_preflight():
    """_build_heart_for_eval must call run_migrations before the schema
    preflight so the baked eval-DB image never fails on new ORM columns.

    Verifies call ORDER: run_migrations first, assert_eval_db_schema second.
    The preflight is an assertion gate — if migrations haven't run it raises
    EvalDBSchemaDriftError and aborts the eval run.  By making the preflight
    raise after recording its call we can confirm both calls happened (in the
    right order) without needing a live database or a fully initialised Heart.
    """
    call_order: list[str] = []

    async def _fake_seed(_engine):
        call_order.append("seed")
        return []

    async def _fake_run_migrations(_engine):
        call_order.append("migrate")
        return []

    async def _fake_preflight(_db):
        call_order.append("preflight")
        # Raise so _build_heart_for_eval aborts before Heart construction;
        # this is the realistic failure mode when the image is stale.
        raise EvalDBSchemaDriftError("test: missing column")

    with (
        patch(
            "nous_eval.schema_preflight.seed_baked_migration_history",
            side_effect=_fake_seed,
        ),
        patch(
            "nous.storage.migrator.run_migrations",
            side_effect=_fake_run_migrations,
        ),
        patch(
            "nous_eval.schema_preflight.assert_eval_db_schema_matches_orm",
            side_effect=_fake_preflight,
        ),
    ):
        from nous_eval.retrieval_runner import _build_heart_for_eval

        db = MagicMock()
        db.engine = MagicMock()
        settings = MagicMock()
        settings.query_expansion_enabled = False

        with pytest.raises(EvalDBSchemaDriftError):
            async with _build_heart_for_eval(db, settings):
                pass  # pragma: no cover

    # Seeding must precede migrate: on a baked image with no history,
    # run_migrations would otherwise replay every migration and fail.
    assert call_order == ["seed", "migrate", "preflight"], call_order


async def test_lme_ingest_seeds_history_before_migrating():
    """ingest_longmemeval hits the same baked scratch DB, so it must seed the
    migration history before run_migrations, exactly like the harness."""
    call_order: list[str] = []

    async def _fake_seed(_engine):
        call_order.append("seed")
        return []

    async def _fake_run_migrations(_engine):
        call_order.append("migrate")
        return []

    async def _fake_preflight(_db):
        call_order.append("preflight")
        raise EvalDBSchemaDriftError("test: abort before ingest")

    settings = MagicMock()
    settings.openai_api_key = "sk-test"
    settings.model_copy.return_value = settings
    fake_db = MagicMock()
    fake_db.connect = AsyncMock()
    fake_db.disconnect = AsyncMock()

    with (
        patch("nous_eval.ingest._settings_for_ingest", return_value=settings),
        patch("nous.storage.database.Database", return_value=fake_db),
        patch(
            "nous_eval.schema_preflight.seed_baked_migration_history",
            side_effect=_fake_seed,
        ),
        patch("nous.storage.migrator.run_migrations", side_effect=_fake_run_migrations),
        patch(
            "nous_eval.schema_preflight.assert_eval_db_schema_matches_orm",
            side_effect=_fake_preflight,
        ),
    ):
        from nous_eval.ingest_longmemeval import _replay_sessions_into_scratch

        with pytest.raises(EvalDBSchemaDriftError):
            await _replay_sessions_into_scratch([], "postgresql+asyncpg://x/y")

    assert call_order == ["seed", "migrate", "preflight"], call_order
