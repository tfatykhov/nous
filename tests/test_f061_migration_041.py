"""F061 PR-1: tests for migration 041 (subtask hardening schema).

Verifies the new columns, FK constraint, and indexes are present after the
migrator runs. The migrator is invoked by the `db` fixture's session-scoped
setup, so by the time these tests run, migration 041 has already been applied
against the test database.
"""

from __future__ import annotations

from sqlalchemy import text


class TestMigration041Schema:
    """Verify migration 041 produced the expected schema."""

    async def test_new_columns_exist(self, db) -> None:
        """All 9 new columns are present on heart.subtasks."""
        async with db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema='heart' AND table_name='subtasks' "
                    "ORDER BY column_name"
                )
            )
            cols = {row[0]: (row[1], row[2], row[3]) for row in result}

        # report_jsonb: JSONB, NULL, no default
        assert cols["report_jsonb"][0] == "jsonb"
        assert cols["report_jsonb"][1] == "YES"

        # final_outcome: VARCHAR(32), NULL, no default
        assert cols["final_outcome"][0] == "character varying"
        assert cols["final_outcome"][1] == "YES"

        # attempts: INTEGER, NOT NULL, default 0
        assert cols["attempts"][0] == "integer"
        assert cols["attempts"][1] == "NO"
        assert cols["attempts"][2] == "0"

        # tokens_in / tokens_out: INTEGER, NOT NULL, default 0
        assert cols["tokens_in"][0] == "integer"
        assert cols["tokens_in"][1] == "NO"
        assert cols["tokens_in"][2] == "0"
        assert cols["tokens_out"][0] == "integer"
        assert cols["tokens_out"][1] == "NO"
        assert cols["tokens_out"][2] == "0"

        # tool_calls_made: INTEGER, NOT NULL, default 0
        assert cols["tool_calls_made"][0] == "integer"
        assert cols["tool_calls_made"][1] == "NO"
        assert cols["tool_calls_made"][2] == "0"

        # output_format / success_criteria: TEXT, NULL
        assert cols["output_format"][0] == "text"
        assert cols["output_format"][1] == "YES"
        assert cols["success_criteria"][0] == "text"
        assert cols["success_criteria"][1] == "YES"

        # dag_node_id: UUID, NULL
        assert cols["dag_node_id"][0] == "uuid"
        assert cols["dag_node_id"][1] == "YES"

    async def test_fk_constraint_present(self, db) -> None:
        """fk_subtasks_dag_node FK exists with ON DELETE SET NULL."""
        async with db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT con.conname, con.confdeltype "
                    "FROM pg_constraint con "
                    "JOIN pg_class cl ON cl.oid = con.conrelid "
                    "JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
                    "WHERE ns.nspname='heart' AND cl.relname='subtasks' "
                    "AND con.contype='f' AND con.conname='fk_subtasks_dag_node'"
                )
            )
            row = result.first()
        assert row is not None, "fk_subtasks_dag_node not found"
        # confdeltype is a single-char column ("char" type) returned as bytes by asyncpg.
        # 'n' = SET NULL, 'a' = NO ACTION, 'r' = RESTRICT, 'c' = CASCADE, 'd' = SET DEFAULT.
        deltype = row[1] if isinstance(row[1], str) else row[1].decode()
        assert deltype == "n", f"expected ON DELETE SET NULL ('n'), got '{deltype}'"

    async def test_indexes_present(self, db) -> None:
        """Both new indexes exist."""
        async with db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='heart' AND tablename='subtasks'"
                )
            )
            names = {row[0] for row in result}
        assert "idx_subtasks_outcome" in names
        assert "idx_subtasks_dag_node" in names

    async def test_dag_nodes_table_precondition(self, db) -> None:
        """nous_system.dag_nodes must exist (FK target)."""
        async with db.engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='nous_system' AND table_name='dag_nodes'"
                )
            )
            assert result.first() is not None, (
                "FK target nous_system.dag_nodes is missing — migration 032 (DAG "
                "orchestration) must run before 041."
            )

    async def test_migration_is_idempotent(self, db) -> None:
        """Re-applying 041 against an already-migrated DB must not error.

        The migrator at nous/storage/migrator.py skips already-applied
        versions, so direct re-execution would not happen in production. But
        the SQL itself uses IF NOT EXISTS for columns/indexes and
        DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT for the FK precisely so
        that manual re-runs (test setup teardown, schema rebuilds) are safe.
        This test executes each statement a second time and asserts no error.
        """
        from pathlib import Path

        from nous.storage.migrator import _split_sql_statements

        sql_path = (
            Path(__file__).resolve().parents[1]
            / "sql"
            / "migrations"
            / "041_subtask_hardening.sql"
        )
        statements = _split_sql_statements(sql_path.read_text(encoding="utf-8"))
        assert statements, "041 SQL split produced zero statements"

        async with db.engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))  # must not raise
