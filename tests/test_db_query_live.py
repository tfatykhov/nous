"""The ``nous_reader`` login (scripts/ops/db_roles.sql) and the ``db_query`` tool, against the live role.

Every proof here is SQL run against the real role: catalog queries read through the ``db`` fixture (the application
login), and the tool's own tests log in as ``nous_reader``. The role exists only where db_roles.sql has run (the
"DB roles" CI job, which sets NOUS_REQUIRE_READER_ROLE=1 so that a missing role fails); elsewhere these tests skip.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres_only

# The column names the reader must never read; scripts/ops/db_roles.sql refuses to grant them unless excluded.
CREDENTIAL_NAME = (
    r"(^|_)(token|secret|password|passwd|credential|credentials)(_|$)|(api|private|secret|access|auth)_?key|fcm"
)
SPEC_TABLES = {
    "brain.intentions",
    "brain.intention_arrivals",
    "brain.intention_proposals",
    "brain.decisions",
    "heart.subtasks",
    "heart.schedules",
    "heart.facts",
    "heart.episodes",
    "nous_system.execution_dags",
    "nous_system.dag_nodes",
    "nous_system.dag_edges",
    "nous_system.dynamic_checks",
    "nous_system.execution_ledger",
    "nous_system.events",
}


async def _rows(db, sql: str, **params) -> list[tuple]:
    async with db.engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]


# -- the role's privileges, from the catalog ------------------------------------------------------------------------


_SCHEMAS = "n.nspname IN ('brain', 'heart', 'nous_system', 'scoring', 'public')"
_RELATIONS = f"{_SCHEMAS} AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"


async def test_reader_holds_select_only_everywhere(db, reader_role):
    offenders = await _rows(
        db,
        f"""
        SELECT n.nspname || '.' || c.relname || ' table ' || p
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) p
         WHERE {_RELATIONS} AND has_table_privilege(:r, c.oid, p)
        UNION ALL
        SELECT n.nspname || '.' || c.relname || ' table-level SELECT'
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE {_RELATIONS} AND has_table_privilege(:r, c.oid, 'SELECT')
        UNION ALL
        SELECT n.nspname || '.' || c.relname || ' column ' || p
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) p
         WHERE {_RELATIONS} AND has_any_column_privilege(:r, c.oid, p)
        UNION ALL
        SELECT n.nspname || '.' || c.relname || ' sequence ' || p
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         CROSS JOIN unnest(ARRAY['USAGE', 'SELECT', 'UPDATE']) p
         WHERE {_SCHEMAS} AND c.relkind = 'S' AND has_sequence_privilege(:r, c.oid, p)
        UNION ALL
        SELECT p.oid::regprocedure::text || ' EXECUTE'
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname IN ('brain', 'heart', 'nous_system', 'scoring')
           AND has_function_privilege(:r, p.oid, 'EXECUTE')
        """,
        r=reader_role,
    )
    assert offenders == [], offenders


async def test_reader_reads_nothing_in_scoring_or_a2ui(db, reader_role):
    [(usage,)] = await _rows(db, "SELECT has_schema_privilege(:r, 'scoring', 'USAGE')", r=reader_role)
    assert usage is False
    offenders = await _rows(
        db,
        """
        SELECT n.nspname || '.' || c.relname
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND (n.nspname = 'scoring' OR c.relname LIKE 'a2ui\\_%')
           AND has_any_column_privilege(:r, c.oid, 'SELECT')
        """,
        r=reader_role,
    )
    assert offenders == [], offenders
    # the check sees the relations it guards (otherwise it could not fail)
    [(guarded,)] = await _rows(
        db,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND (n.nspname = 'scoring' OR c.relname = 'a2ui_push_installations')",
    )
    assert guarded >= 3


async def test_reader_reads_nothing_in_public(db, reader_role):
    offenders = await _rows(
        db,
        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND has_any_column_privilege(:r, c.oid, 'SELECT')",
        r=reader_role,
    )
    assert offenders == [], offenders


async def test_reader_reads_no_credential_named_column(db, reader_role):
    named = await _rows(
        db,
        """
        SELECT n.nspname || '.' || c.relname || '.' || a.attname,
               has_column_privilege(:r, c.oid, a.attnum, 'SELECT')
          FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname IN ('brain', 'heart', 'nous_system', 'scoring') AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND a.attnum > 0 AND NOT a.attisdropped AND a.attname ~ :pattern
        """,
        r=reader_role,
        pattern=CREDENTIAL_NAME,
    )
    # a column of a granted table matches, so the rule is exercised
    assert "brain.intention_arrivals.claim_token" in {name for name, _ in named}, named
    assert [name for name, readable in named if readable] == []


async def test_reader_reads_no_embedding_or_tsvector_column(db, reader_role):
    typed = await _rows(
        db,
        """
        SELECT n.nspname || '.' || c.relname || '.' || a.attname, t.typname,
               has_column_privilege(:r, c.oid, a.attnum, 'SELECT')
          FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_type t ON t.oid = a.atttypid
         WHERE n.nspname IN ('brain', 'heart', 'nous_system', 'scoring') AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND a.attnum > 0 AND NOT a.attisdropped AND t.typname IN ('vector', 'halfvec', 'sparsevec', 'tsvector')
        """,
        r=reader_role,
    )
    names = {name for name, _, _ in typed}
    assert "heart.facts.embedding" in names and any(t == "tsvector" for _, t, _ in typed), typed
    assert [name for name, _, readable in typed if readable] == []


async def test_reader_relations_are_plain_tables_without_row_policies(db, reader_role):
    # a view runs with its owner's privileges, and a row policy is code the application role defines
    offenders = await _rows(
        db,
        """
        SELECT n.nspname || '.' || c.relname
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE has_any_column_privilege(:r, c.oid, 'SELECT') AND n.nspname <> 'pg_catalog'
           AND n.nspname <> 'information_schema'
           AND (c.relkind <> 'r' OR c.relrowsecurity OR c.relforcerowsecurity
                OR EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid))
        """,
        r=reader_role,
    )
    assert offenders == [], offenders


async def test_reader_is_isolated_from_other_roles(db, reader_role):
    # the reader can become no other role, and no role is a member of it (a superuser holds every role's
    # privileges by its attribute, not by membership, so membership is read from pg_auth_members)
    offenders = await _rows(
        db,
        """
        SELECT r.rolname FROM pg_roles r WHERE r.rolname <> :r AND pg_has_role(:r, r.oid, 'MEMBER')
        UNION ALL
        SELECT g.rolname || ' > ' || m.rolname
          FROM pg_auth_members am JOIN pg_roles g ON g.oid = am.roleid JOIN pg_roles m ON m.oid = am.member
         WHERE g.rolname = :r OR m.rolname = :r
        """,
        r=reader_role,
    )
    assert offenders == [], offenders
    [attrs] = await _rows(
        db,
        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolcanlogin, rolinherit "
        "FROM pg_roles WHERE rolname = :r",
        r=reader_role,
    )
    assert attrs[:5] == (False, False, False, False, False) and attrs[5] is True, attrs


async def test_reader_settings_and_limits(db, reader_role):
    [(config, limit, until)] = await _rows(
        db,
        """
        SELECT (SELECT s.setconfig FROM pg_db_role_setting s WHERE s.setrole = r.oid AND s.setdatabase = 0),
               r.rolconnlimit, r.rolvaliduntil::text
          FROM pg_roles r WHERE r.rolname = :r
        """,
        r=reader_role,
    )
    assert sorted(config) == sorted(
        [
            "search_path=pg_catalog, brain, heart, nous_system",
            "row_security=off",
            "default_transaction_read_only=on",
            "statement_timeout=5s",
            "lock_timeout=2s",
            "idle_in_transaction_session_timeout=15s",
            "temp_file_limit=256MB",
        ]
    ), config
    assert limit == 6 and until == "infinity"
    per_database = await _rows(
        db,
        "SELECT s.setdatabase FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
        "WHERE r.rolname = :r AND s.setdatabase <> 0",
        r=reader_role,
    )
    assert per_database == []


async def test_no_extension_or_server_can_open_a_connection(db, reader_role):
    [(extensions, servers, large_objects)] = await _rows(
        db,
        "SELECT (SELECT count(*) FROM pg_extension WHERE extname IN ('dblink', 'postgres_fdw', 'file_fdw')), "
        "(SELECT count(*) FROM pg_foreign_server), (SELECT count(*) FROM pg_largeobject_metadata)",
    )
    assert (extensions, servers, large_objects) == (0, 0, 0)


async def test_reader_can_read_at_least_one_column_of_each_spec_table(db, reader_role):
    # the positive control: the negative tests above cannot pass on an empty grant
    readable = await _rows(
        db,
        """
        SELECT n.nspname || '.' || c.relname
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname IN ('brain', 'heart', 'nous_system') AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND has_any_column_privilege(:r, c.oid, 'SELECT')
        """,
        r=reader_role,
    )
    assert {name for (name,) in readable} == SPEC_TABLES
    [(content, embedding)] = await _rows(
        db,
        "SELECT has_column_privilege(:r, 'heart.facts', 'content', 'SELECT'), "
        "has_column_privilege(:r, 'heart.facts', 'embedding', 'SELECT')",
        r=reader_role,
    )
    assert (content, embedding) == (True, False)
