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
from unittest.mock import AsyncMock, MagicMock

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
