"""F108 Phase 1 (recording), migration 100: the tool-call and turn tables, their settings, and the reader's grant.

The table tests run against real Postgres (the migration is the schema). The reader test needs the ``nous_reader``
login that only scripts/ops/db_roles.sql creates: it skips where the role does not exist and fails in the
"DB roles" CI job when it is missing (the ``reader_role`` fixture, as in tests/test_db_query_live.py).
"""

from __future__ import annotations

import re
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nous.api.db_query import reader_connect_args
from nous.config import Settings

# The column names scripts/ops/db_roles.sql refuses to grant the reader unless excluded (its section 5d regex).
CREDENTIAL_NAME = (
    r"(^|_)(token|secret|password|passwd|credential|credentials)(_|$)|(api|private|secret|access|auth)_?key|fcm"
)
# Every column that is NOT NULL without a default: the least a writer must give.
_REQUIRED = {
    "agent_id": "x",
    "call_index": 0,
    "work_kind": "conversation",
    "tool_name": "recall_deep",
    "tool_name_called": "recall",
    "outcome": "ran",
}
_INSERT = (
    "INSERT INTO nous_system.tool_call_log (agent_id, call_index, work_kind, tool_name, tool_name_called, outcome) "
    "VALUES (:agent_id, :call_index, :work_kind, :tool_name, :tool_name_called, :outcome) RETURNING id"
)


def _agent() -> str:
    return f"test-awv-{uuid.uuid4().hex[:12]}"


async def _delete(db, agent: str) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM nous_system.tool_call_log WHERE agent_id = :a"), {"a": agent})
        await conn.execute(text("DELETE FROM nous_system.agent_turn_log WHERE agent_id = :a"), {"a": agent})


@pytest.mark.postgres_only
async def test_tool_call_log_columns(db):
    agent = _agent()
    try:
        async with db.engine.begin() as conn:
            row_id = (await conn.execute(text(_INSERT), {**_REQUIRED, "agent_id": agent})).scalar_one()
        async with db.engine.connect() as conn:
            row = (
                (await conn.execute(text("SELECT * FROM nous_system.tool_call_log WHERE id = :id"), {"id": row_id}))
                .mappings()
                .one()
            )
        assert isinstance(row["id"], uuid.UUID)
        assert (row["agent_id"], row["call_index"], row["work_kind"]) == (agent, 0, "conversation")
        assert (row["tool_name"], row["tool_name_called"], row["outcome"]) == ("recall_deep", "recall", "ran")
        # the defaults
        assert (row["input_text"], row["result_preview"], row["is_error"]) == ("", "", False)
        assert row["started_at"] is not None and row["started_at"].tzinfo is not None
        # everything else is optional
        for column in (
            "session_id",
            "parent_session_id",
            "turn_id",
            "turn_number",
            "context_kind",
            "subtask_id",
            "dag_id",
            "dag_node_id",
            "reason",
            "error_text",
            "refused_by",
            "duration_ms",
            "ledger_entry_id",
        ):
            assert row[column] is None, column

        # outcome and call_index are required
        for missing in ("outcome", "call_index"):
            columns = [c for c in _REQUIRED if c != missing]
            sql = (
                f"INSERT INTO nous_system.tool_call_log ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})"
            )
            with pytest.raises(IntegrityError):
                async with db.engine.begin() as conn:
                    await conn.execute(text(sql), {c: ({**_REQUIRED, "agent_id": agent})[c] for c in columns})
    finally:
        await _delete(db, agent)


@pytest.mark.postgres_only
async def test_the_indexes_and_no_credential_named_column(db):
    async with db.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'nous_system' "
                    "AND tablename IN ('tool_call_log', 'agent_turn_log')"
                )
            )
        ).all()
        names = (
            (
                await conn.execute(
                    text(
                        "SELECT table_name || '.' || column_name FROM information_schema.columns "
                        "WHERE table_schema = 'nous_system' AND table_name IN ('tool_call_log', 'agent_turn_log')"
                    )
                )
            )
            .scalars()
            .all()
        )
    indexes: dict[str, set[str]] = {}
    for table, definition in rows:
        indexes.setdefault(table, set()).add(definition.split(" USING btree ", 1)[1])
    assert indexes["tool_call_log"] == {
        "(id)",
        "(turn_id, call_index)",
        "(agent_id, started_at DESC)",
        "(agent_id, tool_name, started_at DESC)",
        "(agent_id, session_id, started_at DESC)",
    }, indexes
    assert indexes["agent_turn_log"] == {
        "(id)",
        "(agent_id, session_id, turn_number)",
        "(agent_id, started_at DESC)",
    }, indexes
    # db_roles.sql section 5d refuses to grant a credential-looking column unless excluded
    assert len(names) > 20 and not [n for n in names if re.search(CREDENTIAL_NAME, n.split(".", 1)[1])], names


@pytest.mark.postgres_only
async def test_agent_turn_log_pk_is_turn_id(db):
    agent = _agent()
    turn_id = uuid.uuid4()
    try:
        async with db.engine.connect() as conn:
            [pk] = (
                await conn.execute(
                    text(
                        "SELECT array_agg(a.attname::text) FROM pg_index i "
                        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                        "WHERE i.indrelid = 'nous_system.agent_turn_log'::regclass AND i.indisprimary"
                    )
                )
            ).one()
        assert pk == ["id"]
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO nous_system.agent_turn_log (id, agent_id, work_kind) VALUES (:id, :a, 'conversation')"
                ),
                {"id": turn_id, "a": agent},
            )
            await conn.execute(text(_INSERT), {**_REQUIRED, "agent_id": agent})
            await conn.execute(
                text("UPDATE nous_system.tool_call_log SET turn_id = :t WHERE agent_id = :a"),
                {"t": turn_id, "a": agent},
            )
        async with db.engine.connect() as conn:
            joined = (
                await conn.execute(
                    text(
                        "SELECT t.id, t.started_at IS NOT NULL, count(c.id) FROM nous_system.agent_turn_log t "
                        "JOIN nous_system.tool_call_log c ON c.turn_id = t.id WHERE t.id = :t GROUP BY t.id"
                    ),
                    {"t": turn_id},
                )
            ).one()
        assert tuple(joined) == (turn_id, True, 1)
        # one row per turn
        with pytest.raises(IntegrityError):
            async with db.engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO nous_system.agent_turn_log (id, agent_id, work_kind) VALUES (:id, :a, 'x')"),
                    {"id": turn_id, "a": agent},
                )
    finally:
        await _delete(db, agent)


def test_settings_defaults():
    fields = Settings.model_fields
    assert fields["tool_call_log_enabled"].default is False
    assert fields["tool_reason_enabled"].default is False
    assert fields["tool_call_retention_days"].default == 30
    assert fields["tool_input_max_chars"].default == 2000
    assert fields["tool_result_preview_chars"].default == 500
    assert fields["tool_reason_max_chars"].default == 500


@pytest.mark.postgres_only
async def test_reader_can_read_tool_call_log_but_not_turn_log(db, reader_role, reader_settings):
    # from the catalog: some column of the tool-call log, no column of the turn log (it holds message text)
    async with db.engine.connect() as conn:
        [(calls, turns)] = (
            await conn.execute(
                text(
                    "SELECT has_any_column_privilege(:r, 'nous_system.tool_call_log', 'SELECT'), "
                    "has_any_column_privilege(:r, 'nous_system.agent_turn_log', 'SELECT')"
                ),
                {"r": reader_role},
            )
        ).all()
    assert (calls, turns) == (True, False)
    # logged in as the reader
    conn = await asyncpg.connect(**reader_connect_args(reader_settings))
    try:
        async with conn.transaction(readonly=True):
            assert await conn.fetchval("SELECT count(*) FROM nous_system.tool_call_log") >= 0
        with pytest.raises(asyncpg.PostgresError) as denied:
            async with conn.transaction(readonly=True):
                await conn.fetch("SELECT count(*) FROM nous_system.agent_turn_log")
        assert denied.value.sqlstate == "42501"
    finally:
        await conn.close()
