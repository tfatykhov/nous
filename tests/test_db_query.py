"""db_query: the statement rules, the result text and the redaction. No database.

The database's own refusals (read-only, privileges, one statement) are proven against the live role in
tests/test_db_query_live.py; the shape check here is a courtesy that gives the agent a clear message first.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from nous.api import db_query
from nous.api.db_query import (
    APPLICATION_NAME,
    DB_QUERY_SCHEMA,
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    MAX_OUTPUT_CHARS,
    MAX_ROWS,
    MAX_SQL_CHARS,
    Cell,
    QueryRefused,
    answer_db_query,
    check_query_shape,
    format_result,
    reader_connect_args,
    register_db_query_tool,
    run_db_query,
)
from nous.api.execution_context import ExecutionContext
from nous.api.runner import FRAME_TOOLS
from nous.api.tool_classes import tool_class
from nous.api.tool_policy import CONTEXT_POLICY, evaluate, internal_only_allowed, internal_only_call_violation
from nous.api.tools import ToolDispatcher
from nous.cognitive import execution_ledger as el
from nous.cognitive import ledger_store
from nous.config import Settings


def _cells(*values: str | None) -> list[Cell]:
    return [Cell(v, None if v is None else len(v)) for v in values]


# -- the statement rules ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select 1", "select 1"),
        ("SELECT id FROM heart.subtasks", "SELECT id FROM heart.subtasks"),
        ("  Select 1  ", "Select 1"),
        ("with x as (select 1) select * from x", "with x as (select 1) select * from x"),
        ("WITH RECURSIVE t(n) AS (SELECT 1) SELECT n FROM t", "WITH RECURSIVE t(n) AS (SELECT 1) SELECT n FROM t"),
        ("-- why\nselect 1", "-- why\nselect 1"),
        ("/* c */ select 1", "/* c */ select 1"),
        ("/* outer /* nested */ still */ select 1", "/* outer /* nested */ still */ select 1"),
        ("select 1;", "select 1"),
        ("select 1 ;  -- done\n", "select 1"),
        ("select 1; /* done */", "select 1"),
    ],
)
def test_shape_accepts_select_and_with(sql, expected):
    assert check_query_shape(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "insert into heart.subtasks default values",
        "update heart.subtasks set status = 'x'",
        "delete from heart.subtasks",
        "drop table heart.subtasks",
        "copy (select 1) to program 'true'",
        "do $$ begin null; end $$",
        "call p()",
        "set role nous_app",
        "explain select 1",
        "(select 1)",
        "values (1)",
        "table heart.subtasks",
        "selectx from t",
        "-- only a comment",
        "",
        "   \n\t ",
        "select 1 " + "x" * MAX_SQL_CHARS,
        "select 'a\x00b'",
    ],
)
def test_shape_refuses_what_is_not_a_select(sql):
    with pytest.raises(QueryRefused) as refused:
        check_query_shape(sql)
    assert "SELECT or WITH" in str(refused.value)


def test_shape_refuses_what_is_not_text():
    with pytest.raises(QueryRefused):
        check_query_shape(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "sql",
    [
        "select 1; select 2",
        "select 1; /* x */ drop table t",
        "select 1;-- c\n delete from t",
        "with d as (select 1) select 1;delete from t",
        "select 'a\\'; select 2",  # a backslash does not escape a quote in a standard string
    ],
)
def test_shape_refuses_a_second_statement(sql):
    with pytest.raises(QueryRefused) as refused:
        check_query_shape(sql)
    assert "one statement" in str(refused.value)


@pytest.mark.parametrize(
    "sql",
    [
        "select 'a;b'",
        "select 'it''s; fine'",
        'select "a;b" from t',
        "select $$a;b$$",
        "select $f$a;b$f$",
        "select $f$ $$ ; $f$",
        "select E'a\\';b'",  # in an escape string a backslash does escape the quote
        "select 1 /* ; */",
        "select 1 -- ;",
        "select ';' as x, $1",
    ],
)
def test_shape_does_not_split_on_semicolons_inside_quotes(sql):
    assert check_query_shape(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "with x as (select 1) delete from heart.subtasks",
        "WITH x AS (SELECT 1) INSERT INTO heart.subtasks SELECT * FROM x",
        "with x as (select 1) update heart.subtasks set status = 'x'",
        "with recursive x(n) as (select 1) merge into t using x on true when matched then delete",
        "with x as (select 1) /* c */ Delete from t",
    ],
)
def test_shape_refuses_a_with_statement_that_writes(sql):
    with pytest.raises(QueryRefused) as refused:
        check_query_shape(sql)
    assert "SELECT or WITH" in str(refused.value) and "writes" in str(refused.value)


@pytest.mark.parametrize(
    "sql",
    [
        "with x as (select 'delete' as w) select * from x",
        'with x as (select 1 as "update") select * from x',
        "with x as (select 1) select * from x where note = 'insert into t'",
        "with x as (select 1) select deleted_at, updated_at, inserted from x",
        # a data-modifying CTE inside the parentheses is the server's to refuse (read-only, no privilege)
        "with d as (delete from t returning 1) select count(*) from d",
    ],
)
def test_shape_lets_the_server_judge_what_a_with_statement_only_reads(sql):
    assert check_query_shape(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "select 'a",
        'select "a',
        "select $$a",
        "select $t$a$u$",
        "select 1 /* x",
        "select 1 /* a /* b */",
        "select (1",
        "select 1)",
        "select 1) as q union all select (2",
    ],
)
def test_shape_refuses_unterminated_quotes_and_unbalanced_parentheses(sql):
    with pytest.raises(QueryRefused) as refused:
        check_query_shape(sql)
    assert "SELECT or WITH" in str(refused.value)


# -- the result text -------------------------------------------------------------------------------------------------


def test_format_result_is_compact_text():
    text = format_result(["id", "note", "n"], [_cells("a1", None, "3"), _cells("b2", "x", "4")], truncated=False)
    assert text.splitlines() == ["id | note | n", "a1 | NULL | 3", "b2 | x | 4", "(2 rows)"]
    assert format_result(["id"], [], truncated=False).splitlines() == ["id", "(0 rows)"]
    capped = format_result(["n"], [_cells(str(i)) for i in range(MAX_ROWS)], truncated=True)
    lines = capped.splitlines()
    assert len(lines) == 1 + MAX_ROWS + 1
    assert f"first {MAX_ROWS} rows" in lines[-1] and "more" in lines[-1]


def test_format_result_with_no_columns():
    assert format_result([], [[], []], truncated=False).splitlines() == ["(no columns)", "", "", "(2 rows)"]


def test_cells_are_clipped_after_redaction():
    long = format_result(["c"], [_cells("x" * 2000)], truncated=False).splitlines()[1]
    assert long == "x" * MAX_CELL_CHARS + "…[+1500 chars]"
    secret = "api_key=sk-abcdefghijklmnopqrstuvwxyz0123"
    assert 480 < MAX_CELL_CHARS < 480 + len(secret)  # the clip falls inside the key
    text = format_result(["c"], [_cells("x" * 480 + secret)], truncated=False)
    # redaction ran on the whole cell before the clip, so no part of the key is shown
    assert "sk-" not in text and "abcdefghij" not in text and "0123" not in text
    assert "api_key=[REDACTED]" in text


def test_a_cell_cut_by_the_server_hides_a_secret_the_cut_split():
    # The server sends at most a window of each cell. A secret that the window's end splits matches no pattern, and
    # masking earlier in the window pulls it toward the front: the last scan-pad characters of a cut window are
    # never shown.
    window = "token=" + "A" * 1600 + " " + "q" * 370 + " sk-PARTIAL"
    whole = format_result(["c"], [[Cell(window, len(window))]], truncated=False)
    assert "PARTIAL" in whole  # the control: as the whole value, its end is shown
    cut = format_result(["c"], [[Cell(window, len(window) + 5000)]], truncated=False)
    assert "PARTIAL" not in cut and "token=[REDACTED]" in cut
    assert re.search(r"…\[\+\d+ chars\]$", cut.splitlines()[1]), cut


def test_newlines_in_a_cell_do_not_forge_a_row():
    text = format_result(["a", "b"], [_cells("x\ny | z", "p\r\nq r\x0bs")], truncated=False)
    lines = text.splitlines()
    assert lines[1] == "x\\ny | z | p\\r\\nq\\u2028r\\x0bs"
    assert len(lines) == 3
    assert format_result(["bad\nname"], [], truncated=False).splitlines()[0] == "bad\\nname"


def test_own_env_secrets_are_masked(monkeypatch):
    monkeypatch.setenv("NOUS_TEST_TOKEN", "qwertyuiop12")
    ledger_store._own_secrets.cache_clear()
    try:
        text = format_result(["c"], [_cells("the value qwertyuiop12 leaked")], truncated=False)
        assert "qwertyuiop12" not in text and ledger_store.SECRET_MASK in text
    finally:
        monkeypatch.delenv("NOUS_TEST_TOKEN")
        ledger_store._own_secrets.cache_clear()


def test_the_whole_output_is_capped():
    rows = [_cells(*["y" * 600] * 60) for _ in range(MAX_ROWS)]
    text = format_result([f"c{i}" for i in range(60)], rows, truncated=True)
    assert len(text) <= MAX_OUTPUT_CHARS
    last = text.splitlines()[-1]
    assert f"{MAX_OUTPUT_CHARS}" in last and "fewer columns" in last


def test_redact_output_is_the_ledger_redaction():
    sample = "Bearer abc.def password=hunter22 sk-abcdefghijklmnopqrstuvwxyz plain text"
    assert ledger_store.redact_output(sample) == ledger_store._redact_detail(sample)
    assert "hunter22" not in ledger_store.redact_output(sample)
    assert ledger_store.redaction_scan_pad() >= 256


# -- the login -------------------------------------------------------------------------------------------------------


def _settings(**over) -> Settings:
    # the DB_* fields are read by their unprefixed aliases
    base = {"DB_HOST": "dbhost", "DB_PORT": 6543, "DB_USER": "appuser", "DB_PASSWORD": "apppassword1", "DB_NAME": "nx"}
    return Settings(**{**base, **over})


def test_the_reader_login_never_uses_the_application_login():
    args = reader_connect_args(_settings(reader_db_user="someone", reader_db_password="readerpassword1"))
    assert args["user"] == "someone" and args["password"] == "readerpassword1"
    assert (args["host"], args["port"], args["database"]) == ("dbhost", 6543, "nx")
    assert "appuser" not in repr(args) and "apppassword1" not in repr(args)
    assert args["server_settings"] == {"application_name": APPLICATION_NAME}
    assert 0 < args["timeout"] <= 5


async def test_not_configured_without_a_password_and_no_connection_is_opened(monkeypatch):
    calls = []

    async def connect(**kwargs):
        calls.append(kwargs)
        raise AssertionError("connected")

    monkeypatch.setattr(db_query.asyncpg, "connect", connect)
    out = await run_db_query(_settings(reader_db_password=""), "select 1")
    assert out.startswith("Error: ") and "not configured" in out and "NOUS_READER_DB_PASSWORD" in out
    assert calls == []


async def test_a_refused_shape_opens_no_connection(monkeypatch):
    async def connect(**kwargs):
        raise AssertionError("connected")

    monkeypatch.setattr(db_query.asyncpg, "connect", connect)
    out = await run_db_query(_settings(reader_db_password="readerpassword1"), "delete from heart.subtasks")
    assert out.startswith("Error: ") and "SELECT or WITH" in out


async def test_a_failed_login_is_one_line_without_the_password(monkeypatch):
    async def connect(**kwargs):
        raise OSError(f"could not reach {kwargs['host']} with {kwargs['password']}\nsecond line")

    monkeypatch.setattr(db_query.asyncpg, "connect", connect)
    out = await run_db_query(_settings(reader_db_password="readerpassword1"), "select 1")
    assert out.startswith("Error: ") and "\n" not in out
    assert "readerpassword1" not in out and "someone" not in out and "nous_reader" in out


# -- registration and offering ---------------------------------------------------------------------------------------


async def test_not_registered_without_a_password(monkeypatch):
    calls = []

    async def connect(**kwargs):
        calls.append(kwargs)
        raise AssertionError("connected")

    monkeypatch.setattr(db_query.asyncpg, "connect", connect)
    dispatcher = ToolDispatcher()
    assert await register_db_query_tool(dispatcher, _settings(reader_db_password="")) is False
    assert not dispatcher.is_registered("db_query") and calls == []


async def test_not_registered_when_the_probe_fails(monkeypatch, caplog):
    async def connect(**kwargs):
        raise OSError(f"refused for {kwargs['password']}")

    monkeypatch.setattr(db_query.asyncpg, "connect", connect)
    dispatcher = ToolDispatcher()
    with caplog.at_level("WARNING", logger="nous.api.db_query"):
        assert await register_db_query_tool(dispatcher, _settings(reader_db_password="readerpassword1")) is False
    assert not dispatcher.is_registered("db_query")
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "db_query" in warnings[0] and "readerpassword1" not in warnings[0], warnings


async def test_the_handler_reports_a_failure_as_an_error_and_never_raises(monkeypatch):
    async def probe_ok(settings, sql):
        return ["?column?"], [[Cell("1", 1)]], False

    monkeypatch.setattr(db_query, "execute_readonly", probe_ok)
    dispatcher = ToolDispatcher()
    assert await register_db_query_tool(dispatcher, _settings(reader_db_password="readerpassword1")) is True
    assert await dispatcher.dispatch("db_query", {"sql": "select 1"}) == ("?column?\n1\n(1 row)", False)

    async def boom(settings, sql):
        raise RuntimeError("boom")

    monkeypatch.setattr(db_query, "execute_readonly", boom)
    text, is_error = await dispatcher.dispatch("db_query", {"sql": "select 1"})
    assert is_error and text.startswith("Error: ")
    text, is_error = await dispatcher.dispatch("db_query", {"sql": "update heart.subtasks set status = 'x'"})
    assert is_error and "SELECT or WITH" in text


async def test_a_result_that_reads_like_an_error_is_not_one(monkeypatch):
    # the error flag comes from how the call ended, not from the text: a column may be named "Error: x"
    async def named_like_an_error(settings, sql):
        return ["Error: x"], [[Cell("1", 1)]], False

    monkeypatch.setattr(db_query, "execute_readonly", named_like_an_error)
    dispatcher = ToolDispatcher()
    assert await register_db_query_tool(dispatcher, _settings(reader_db_password="readerpassword1")) is True
    assert await dispatcher.dispatch("db_query", {"sql": 'select 1 as "Error: x"'}) == ("Error: x\n1\n(1 row)", False)
    answer = await answer_db_query(_settings(reader_db_password="readerpassword1"), 'select 1 as "Error: x"')
    assert answer == ("Error: x\n1\n(1 row)", False)


@pytest.mark.postgres_only
async def test_registered_when_the_probe_passes(reader_settings):
    dispatcher = ToolDispatcher()
    assert await register_db_query_tool(dispatcher, reader_settings) is True
    assert dispatcher.is_registered("db_query")
    text, is_error = await dispatcher.dispatch("db_query", {"sql": "select 1 as one"})
    assert (text, is_error) == ("one\n1\n(1 row)", False)


def test_db_query_is_a_classified_read():
    assert tool_class("db_query").side_effect == "none"
    assert "db_query" in el.READ_TOOLS
    # a read leaves no row in the durable ledger, so it keeps no argument policy there either
    assert el.classify_side_effect("db_query", {"sql": "select 1"}) == "none"


def _lineage(kind: str) -> ExecutionContext:
    return ExecutionContext(
        kind=kind,
        session_id="s",
        authority="internal_only",
        intention_id=uuid.UUID(int=1),
        root_intention_id=uuid.UUID(int=2),
    )


@pytest.mark.parametrize("kind", ["continuation", "subtask", "dag_node"])
@pytest.mark.parametrize("lineage_shell", [False, True])
def test_a_lineage_turn_is_offered_db_query(kind, lineage_shell, tmp_path):
    ctx = _lineage(kind)
    assert internal_only_allowed("db_query", ctx=ctx, lineage_shell=lineage_shell) is True
    violation = internal_only_call_violation(
        ctx, "db_query", {"sql": "select 1"}, workspace_dir=str(tmp_path), lineage_shell=lineage_shell
    )
    assert violation is None


def test_which_contexts_and_frames_receive_db_query():
    # Pinned on purpose: a read is allowed wherever reads are, so narrowing db_query later is one edit here, with
    # this test to guide it. evaluate() returns None when a context may make the call.
    def context(kind: str) -> ExecutionContext:
        if kind == "continuation":
            return _lineage(kind)
        if kind == "approved_action":  # one owner-approved call of the declared tool
            return ExecutionContext(
                kind=kind, session_id="s", proposal_id=uuid.UUID(int=3), declared_tools=("db_query",)
            )
        return ExecutionContext(kind=kind, session_id="s")

    receive = {kind for kind in CONTEXT_POLICY if evaluate(context(kind), "db_query", {"sql": "select 1"}) is None}
    assert receive == {
        "interactive",
        "mcp",
        "subtask",
        "dag_node",
        "scheduled",
        "agent_action",
        "dag_summary",
        "heartbeat_triage",
        "heartbeat_check",
        "heartbeat_callback",
        "background",
        "continuation",
        "approved_action",
    }
    frames = {frame for frame, tools in FRAME_TOOLS.items() if "db_query" in tools or tools == ["*"]}
    assert frames == {"conversation", "question", "decision", "debug", "task"}


def test_the_tool_description_teaches_the_limits():
    description = DB_QUERY_SCHEMA["description"]
    for needed in (
        f"{MAX_ROWS} rows",
        f"{MAX_CELL_CHARS} characters",
        f"{MAX_COLUMNS} columns",
        "one statement",
        "schema-qualified",
        "information_schema.columns",
        "not instructions",
        "SELECT id, status, task FROM heart.subtasks ORDER BY created_at DESC LIMIT 10",
        "heart.facts",
        "nous_system.execution_ledger",
    ):
        assert needed in description, needed
    assert DB_QUERY_SCHEMA["required"] == ["sql"] and DB_QUERY_SCHEMA["properties"]["sql"]["type"] == "string"


def test_compose_passes_the_reader_login_to_the_application():
    # a deployment's compose lists the environment explicitly: without these lines the tool silently stays off
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    service = compose.split("\n  nous:\n", 1)[1].split("\n  nous-", 1)[0]
    assert "      - NOUS_READER_DB_USER=${NOUS_READER_DB_USER:-nous_reader}\n" in service
    assert "      - NOUS_READER_DB_PASSWORD=${NOUS_READER_DB_PASSWORD:-}\n" in service
    assert Settings.model_fields["reader_db_user"].default == "nous_reader"
    assert Settings.model_fields["reader_db_password"].default == ""
