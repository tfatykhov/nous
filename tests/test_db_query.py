"""db_query: the statement rules, the result text and the redaction. No database.

The database's own refusals (read-only, privileges, one statement) are proven against the live role in
tests/test_db_query_live.py; the shape check here is a courtesy that gives the agent a clear message first.
"""

from __future__ import annotations

import re

import pytest

from nous.api import db_query
from nous.api.db_query import (
    APPLICATION_NAME,
    MAX_CELL_CHARS,
    MAX_OUTPUT_CHARS,
    MAX_ROWS,
    MAX_SQL_CHARS,
    Cell,
    QueryRefused,
    check_query_shape,
    format_result,
    reader_connect_args,
    run_db_query,
)
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
