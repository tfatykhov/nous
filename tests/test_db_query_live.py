"""The ``nous_reader`` login (scripts/ops/db_roles.sql) and the ``db_query`` tool, against the live role.

Every proof here is SQL run against the real role: catalog queries read through the ``db`` fixture (the application
login), and the tool's own tests log in as ``nous_reader``. The role exists only where db_roles.sql has run (the
"DB roles" CI job, which sets NOUS_REQUIRE_READER_ROLE=1 so that a missing role fails); elsewhere these tests skip.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import asyncpg
import pytest
from sqlalchemy import text

from nous.api.db_query import (
    APPLICATION_NAME,
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    MAX_OUTPUT_CHARS,
    MAX_ROWS,
    ReaderError,
    execute_readonly,
    reader_connect_args,
    run_db_query,
)
from nous.cognitive.ledger_store import redaction_scan_pad
from nous.config import Settings

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


# -- the role's limits, logged in as the reader ----------------------------------------------------------------------
# Raw statements on a direct login, so the proof is the role's own and does not depend on the tool's checks or its
# wrapper. A refusal must be a privilege error (42501) or a read-only one (class 25): a syntax or missing-object
# error would prove nothing.

CANARY_AGENT = "test-db-query-canary"


@asynccontextmanager
async def _login(settings: Settings):
    conn = await asyncpg.connect(**reader_connect_args(settings))
    try:
        yield conn
    finally:
        await conn.close()


async def _refusal(settings: Settings, sql: str, *, read_only_txn: bool = True) -> str | None:
    """The SQLSTATE a direct reader login gets for ``sql`` (extended protocol), or None if it ran."""
    async with _login(settings) as conn:
        try:
            if read_only_txn:
                async with conn.transaction(readonly=True):
                    await conn.fetch(sql)
            else:
                await conn.fetch(sql)
        except asyncpg.PostgresError as exc:
            return exc.sqlstate
    return None


def _is_refusal(sqlstate: str | None) -> bool:
    return sqlstate == "42501" or (sqlstate or "").startswith("25")


async def _state(db) -> tuple[int, int, int, str]:
    """What a write would change: the canary's count and status, brain's decisions and relations."""
    [row] = await _rows(
        db,
        "SELECT (SELECT count(*) FROM heart.subtasks WHERE agent_id = :a), "
        "(SELECT count(*) FROM brain.decisions), "
        "(SELECT count(*) FROM pg_class WHERE relnamespace = 'brain'::regnamespace), "
        "(SELECT string_agg(status, ',') FROM heart.subtasks WHERE agent_id = :a)",
        a=CANARY_AGENT,
    )
    return row


@pytest.fixture
async def canary(db, reader_role):
    """A subtask row the write attempts would change, so an unchanged count means something."""
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM heart.subtasks WHERE agent_id = :a"), {"a": CANARY_AGENT})
        await conn.execute(
            text("INSERT INTO heart.subtasks (agent_id, task, status) VALUES (:a, 'db_query canary', 'completed')"),
            {"a": CANARY_AGENT},
        )
    yield
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM heart.subtasks WHERE agent_id = :a"), {"a": CANARY_AGENT})


async def test_the_login_defaults_are_pinned(reader_settings):
    # These are the login's DEFAULTS: a bare session may change every one but temp_file_limit (see the next test).
    # What holds against a bare login is its lack of privilege; read-only and the timeouts of a db_query call come
    # from the tool's own transaction.
    async with _login(reader_settings) as conn:
        shown = {
            name: await conn.fetchval(f"SHOW {name}")
            for name in (
                "transaction_read_only",
                "statement_timeout",
                "lock_timeout",
                "idle_in_transaction_session_timeout",
                "temp_file_limit",
                "row_security",
            )
        }
        assert await conn.fetchrow("SELECT current_user, session_user") == ("nous_reader", "nous_reader")
    assert shown == {
        "transaction_read_only": "on",
        "statement_timeout": "5s",
        "lock_timeout": "2s",
        "idle_in_transaction_session_timeout": "15s",
        "temp_file_limit": "256MB",
        "row_security": "off",
    }


async def test_a_bare_session_can_lift_the_defaults_but_not_the_temp_file_limit(reader_settings):
    async with _login(reader_settings) as conn:
        await conn.execute("SET default_transaction_read_only = off")  # a default, not a restriction
        await conn.execute("SET statement_timeout = 0")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("SET temp_file_limit = '1GB'")


@pytest.mark.parametrize(
    ("sql", "sqlstate"),
    [
        # a large object is the one write that needs no privilege: the tool's READ ONLY transaction refuses it
        ("select lo_create(0)", "25006"),
        ("select lo_from_bytea(0, '\\x00')", "25006"),
        ("select set_config('default_transaction_read_only', 'off', false), lo_create(0)", "25006"),
        ("select set_config('transaction_read_only', 'off', true), lo_create(0)", "25001"),
    ],
)
async def test_the_tool_cannot_create_a_large_object(db, reader_settings, sql, sqlstate):
    out = await run_db_query(reader_settings, sql)
    assert out.startswith(f"Error: {sqlstate} "), out
    [(objects,)] = await _rows(db, "SELECT count(*) FROM pg_largeobject_metadata")
    assert objects == 0


WRITES = [
    "insert into brain.decisions default values",
    "update heart.subtasks set status = 'failed'",
    "delete from heart.subtasks",
    "truncate heart.subtasks",
    "create table brain.zz (i int)",
    "create temp table zz (i int)",
    "alter table heart.subtasks add column zz int",
    "drop table heart.subtasks",
    "comment on table heart.subtasks is 'x'",
    "grant select on heart.subtasks to public",
    "select id from heart.subtasks for update",
    "with x as (select 1) insert into heart.subtasks (agent_id, task) select 'test-db-query-canary', 'zz' from x",
    "with x as (select 1) update heart.subtasks set status = 'failed'",
    "with x as (select 1) delete from heart.subtasks",
    "with d as (delete from heart.subtasks returning 1) select count(*) from d",
    "select * into brain.zz from brain.decisions",
]


@pytest.mark.parametrize("sql", WRITES)
@pytest.mark.parametrize("read_only_txn", [True, False], ids=["in-read-only-txn", "bare-login"])
async def test_the_reader_cannot_write(db, reader_settings, canary, sql, read_only_txn):
    before = await _state(db)
    assert before[0] == 1 and before[3] == "completed"
    state = await _refusal(reader_settings, sql, read_only_txn=read_only_txn)
    assert _is_refusal(state), (sql, state)
    assert await _state(db) == before


@pytest.mark.parametrize(
    "sql",
    [
        "set role nous_app",
        "set session authorization nous_app",
        # the statement that escapes a SET LOCAL ROLE design: switching back to the session user works there
        "select set_config('role', 'nous_app', true),"
        " query_to_xml('select * from scoring.task_scores', true, false, '')",
        "select query_to_xml('select * from scoring.task_scores', true, false, '')",
        "select query_to_xml('select embedding from heart.facts', true, false, '')",
    ],
)
async def test_the_reader_cannot_become_another_role_or_read_through_a_function(reader_settings, sql):
    assert await _refusal(reader_settings, sql) == "42501", sql


async def test_query_to_xml_runs_for_an_allowed_query(reader_settings):
    # the control for the query_to_xml refusals above: the function itself runs for the reader
    async with _login(reader_settings) as conn:
        xml = await conn.fetchval("select query_to_xml('select 1 as x', true, false, '')")
    assert "<x>1</x>" in xml


async def test_reset_role_leaves_the_reader_the_reader(reader_settings):
    async with _login(reader_settings) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            # the simple protocol: two statements in one call
            await conn.execute("reset role; select count(*) from scoring.task_scores")
        assert await conn.fetchrow("SELECT current_user, session_user") == ("nous_reader", "nous_reader")


@pytest.mark.parametrize(
    "sql",
    [
        "select pg_read_file('/etc/hostname')",
        "select lo_import('/etc/hostname')",
        "copy (select 1) to program 'true'",
        "select pg_ls_dir('.')",
        "select pg_reload_conf()",
        "select * from pg_authid",
        "select pg_terminate_backend(pid) from pg_stat_activity where usename = 'nous_app'",
    ],
)
async def test_the_reader_cannot_touch_the_server(db, reader_settings, sql):
    [(app_backends,)] = await _rows(db, "SELECT count(*) FROM pg_stat_activity WHERE usename = current_user")
    state = await _refusal(reader_settings, sql)
    if "pg_terminate_backend" in sql and state is None:
        pass  # no row matched; the backend count below is the proof
    else:
        assert state == "42501", (sql, state)
    [(after,)] = await _rows(db, "SELECT count(*) FROM pg_stat_activity WHERE usename = current_user")
    assert after >= app_backends >= 1


async def test_the_server_alone_refuses_a_second_statement(db, reader_settings, canary):
    before = await _state(db)
    with pytest.raises(ReaderError) as refused:
        await execute_readonly(reader_settings, "select 1; delete from heart.subtasks")  # no shape check here
    assert refused.value.sqlstate == "42601" and "multiple commands" in str(refused.value)
    assert await _state(db) == before


# -- the tool, end to end --------------------------------------------------------------------------------------------


async def test_reader_reads_an_allowed_table(reader_settings):
    text_ = await run_db_query(reader_settings, "select count(*) as n from nous_system.events")
    lines = text_.splitlines()
    assert lines[0] == "n" and lines[1].isdigit() and lines[-1] == "(1 row)", text_


@pytest.mark.parametrize(
    "sql",
    [
        "select * from scoring.task_scores",
        "select * from scoring.scorer_heartbeat",
        "select fcm_token from nous_system.a2ui_push_installations",
        "select embedding from heart.facts",
        "select search_tsv from brain.decisions",
        "select * from heart.facts",
        "select to_jsonb(f) from heart.facts f",
        "select id from heart.facts where embedding is null",
        "select detail from heart.episodes",
        "select claim_token from brain.intention_arrivals",
        "select token_budget from nous_system.execution_dags",
        "select * from nous_system.agent_identity",
        "select * from pg_authid",
    ],
)
async def test_the_tool_cannot_read_an_excluded_place(reader_settings, sql):
    out = await run_db_query(reader_settings, sql)
    assert out.startswith("Error: 42501") and "permission denied" in out, out


async def test_the_tool_refuses_what_is_not_a_select(reader_settings):
    for sql in ("do $$ begin null; end $$", "call pg_sleep(0)", "show statement_timeout", "set role nous_app"):
        out = await run_db_query(reader_settings, sql)
        assert out.startswith("Error: ") and "SELECT or WITH" in out, (sql, out)


async def test_the_star_error_teaches_how_to_list_the_readable_columns(reader_settings):
    out = await run_db_query(reader_settings, "select * from heart.facts")
    assert "information_schema.columns" in out and "name the columns" in out, out
    listing = await run_db_query(
        reader_settings,
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'heart' AND table_name = 'facts'",
    )
    names = set(listing.splitlines()[1:-1])
    assert "content" in names and "embedding" not in names and "search_tsv" not in names, listing


async def test_rows_are_capped_at_200(reader_settings):
    lines = (await run_db_query(reader_settings, "select generate_series(1, 1000) as n")).splitlines()
    assert lines[0] == "n" and lines[1:-1] == [str(i) for i in range(1, MAX_ROWS + 1)]
    assert f"first {MAX_ROWS} rows" in lines[-1]


@pytest.mark.parametrize(
    "sql",
    [
        "select a.n from generate_series(1, 10000) a(n), generate_series(1, 10000) b(n)",
        "with recursive t(n) as (select 1 union all select n + 1 from t where n < 100000000) select n from t",
    ],
    ids=["cross-join-100m", "recursive-100m"],
)
async def test_the_server_stops_after_201_rows(reader_settings, sql):
    # 100 million rows: only the LIMIT in the server-side wrapper ends this inside the statement timeout. Without
    # it the server produces every row and the call ends in 57014, so the content alone tells the two apart.
    lines = (await run_db_query(reader_settings, sql)).splitlines()
    assert lines[0] == "n", lines[:2]
    assert len(lines) == 1 + MAX_ROWS + 1 and all(line.isdigit() for line in lines[1:-1]), lines[-1]
    assert f"first {MAX_ROWS} rows" in lines[-1]


async def test_the_server_cuts_each_cell_before_sending_it(reader_settings):
    # the bound on what crosses the wire: removing the server-side cut makes this cell 5000 characters long
    columns, rows, truncated = await execute_readonly(reader_settings, "select repeat('x', 5000) as c, null as d")
    assert columns == ["c", "d"] and not truncated
    [[cell, null]] = rows
    assert len(cell.text) == 4 * MAX_CELL_CHARS + redaction_scan_pad() and cell.length == 5000
    assert null.text is None
    out = await run_db_query(reader_settings, "select repeat('x', 2000) as c")
    assert out.splitlines()[1] == "x" * MAX_CELL_CHARS + "…[+1500 chars]"


async def test_a_large_generated_set_is_refused_by_the_temp_file_limit(reader_settings):
    # generate_series in FROM is materialised whole before the LIMIT; the login's temp_file_limit stops it
    out = await run_db_query(reader_settings, "select n from generate_series(1, 50000000) n")
    assert out.startswith("Error: 53400 ") and "temp_file_limit" in out, out


async def test_values_render_as_postgresql_text(reader_settings):
    # each column is cast to text on the server: PostgreSQL's own output, in column order, duplicates kept
    out = await run_db_query(
        reader_settings,
        "select array[1, 2] as a, '2026-10-27 10:00:00+00'::timestamptz at time zone 'UTC' as t, true as b, "
        """null::int as n, '{"k": 1}'::jsonb as j, 7 as a""",
    )
    assert out.splitlines() == [
        "a | t | b | n | j | a",
        '{1,2} | 2026-10-27 10:00:00 | true | NULL | {"k": 1} | 7',
        "(1 row)",
    ], out


async def test_column_names_cannot_collide_with_the_wrapper(reader_settings):
    out = await run_db_query(reader_settings, 'select 1 as q, 2 as q0, 3 as c1, 4 as c2, 5 as "c1"')
    assert out.splitlines()[:2] == ["q | q0 | c1 | c2 | c1", "1 | 2 | 3 | 4 | 5"], out


async def test_a_statement_with_no_columns_still_counts_its_rows(reader_settings):
    out = await run_db_query(reader_settings, "select from generate_series(1, 3)")
    assert out.splitlines() == ["(no columns)", "", "", "", "(3 rows)"], out


async def test_a_huge_single_cell_comes_back_cut_with_its_full_length(reader_settings):
    # one 50 MB value: the server casts and cuts it per column, and only the cut crosses the wire
    columns, rows, truncated = await execute_readonly(reader_settings, "select repeat('x', 50000000) as c")
    assert columns == ["c"] and not truncated
    [[cell]] = rows
    assert cell.length == 50_000_000 and len(cell.text) == 4 * MAX_CELL_CHARS + redaction_scan_pad()
    out = await run_db_query(reader_settings, "select repeat('x', 50000000) as c")
    assert out.splitlines()[1] == "x" * MAX_CELL_CHARS + "…[+49999500 chars]", out[:200]


async def test_huge_cells_and_rows_stay_bounded(reader_settings):
    # 300 MB of values: the server sends at most 201 cut rows, and the text stops at the output cap
    started = time.monotonic()
    out = await run_db_query(reader_settings, "select repeat('x', 1000000) as c from generate_series(1, 300)")
    assert time.monotonic() - started < 8
    lines = out.splitlines()
    assert len(out) <= MAX_OUTPUT_CHARS and "output cut" in lines[-1], lines[-1]
    assert len(lines) > 100 and all(line == "x" * MAX_CELL_CHARS + "…[+999500 chars]" for line in lines[1:-1])
    columns, rows, truncated = await execute_readonly(
        reader_settings, "select repeat('x', 1000000) as c from generate_series(1, 300)"
    )
    assert truncated and len(rows) == MAX_ROWS and {row[0].length for row in rows} == {1_000_000}


async def test_too_many_columns_are_refused_before_the_statement_runs(reader_settings):
    columns = ", ".join(["1/0 as a"] + [f"{i} as c{i}" for i in range(MAX_COLUMNS)])
    out = await run_db_query(reader_settings, f"select {columns}")
    assert out.startswith("Error: ") and f"{MAX_COLUMNS} columns" in out and "division by zero" not in out, out


async def test_a_wide_result_stays_under_the_output_cap(reader_settings):
    columns = ", ".join(f"repeat('y', 600) as c{i}" for i in range(MAX_COLUMNS))
    out = await run_db_query(reader_settings, f"select {columns} from generate_series(1, 200)")
    assert len(out) <= MAX_OUTPUT_CHARS and "fewer columns" in out.splitlines()[-1]


async def _no_reader_backend_left(db) -> bool:
    for _ in range(50):
        [(n,)] = await _rows(
            db, "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a", a=APPLICATION_NAME
        )
        if n == 0:
            return True
        await asyncio.sleep(0.2)
    return False


@pytest.mark.parametrize(
    "sql",
    [
        "select pg_sleep(30)",
        # a statement that lifts its own timeout: the timer armed at its start still fires, and the client's
        # backstop stops the call even if it did not
        "select set_config('statement_timeout', '0', true), pg_sleep(30)",
    ],
)
async def test_a_slow_statement_is_stopped(db, reader_settings, sql):
    started = time.monotonic()
    out = await run_db_query(reader_settings, sql)
    assert time.monotonic() - started < 8, out
    assert out.startswith("Error: ") and ("statement timeout" in out or "stopped" in out), out
    assert await _no_reader_backend_left(db)


async def test_no_state_survives_a_call(db, reader_settings):
    out = await run_db_query(
        reader_settings, "select pg_try_advisory_lock(424242) as locked, set_config('statement_timeout', '0', false)"
    )
    assert not out.startswith("Error"), out
    assert await _no_reader_backend_left(db)
    [(locks,)] = await _rows(
        db, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 424242 AND classid = 0"
    )
    assert locks == 0
    out = await run_db_query(reader_settings, "select current_setting('statement_timeout') as t")
    assert out.splitlines()[1] == "5s", out


async def test_secrets_in_a_row_are_redacted(reader_settings):
    out = await run_db_query(reader_settings, "select 'api_key=abc123def456ghi789' as x")
    assert "[REDACTED]" in out and "abc123def456ghi789" not in out


async def test_a_wrong_password_names_the_login_not_the_password(reader_settings):
    wrong = reader_settings.model_copy(update={"reader_db_password": "notthepassword123"})
    out = await run_db_query(wrong, "select 1")
    assert out.startswith("Error: ") and "nous_reader" in out and "notthepassword123" not in out, out


async def test_the_app_login_is_refused_as_the_reader():
    # No reader_role fixture: this is the negative control wherever a database is. The application's login is a
    # superuser in the main test job and nous_app (which can use scoring) in the DB roles job; both are refused
    # before the statement runs, so the canary's division by zero is never reached.
    app = Settings()
    as_app = app.model_copy(update={"reader_db_user": app.db_user, "reader_db_password": app.db_password})
    out = await run_db_query(as_app, "select 1/0 as canary")
    assert out.startswith("Error: ") and "not a restricted reader" in out, out
    assert "division by zero" not in out and app.db_password not in out
