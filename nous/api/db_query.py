"""db_query: the agent's read-only SQL tool.

The agent sends one SELECT (or WITH) statement and gets the rows back as compact text. What it may read is decided
by PostgreSQL, not here: every call logs in as its own role, ``nous_reader``, which ``scripts/ops/db_roles.sql``
grants SELECT on the columns of a fixed list of tables and nothing else, and whose login settings make every
session read-only and time-limited. The checks in this module only give the agent a clear message early and keep
the result small: the statement's shape, the row and cell caps, and the ledger's secret redaction on every cell.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from nous.cognitive.ledger_store import redact_output, redaction_scan_pad

MAX_ROWS = 200
MAX_CELL_CHARS = 500
MAX_SQL_CHARS = 8_000
MAX_OUTPUT_CHARS = 100_000

_RULE = "db_query runs one read-only statement that starts with SELECT or WITH."


class QueryRefused(ValueError):
    """The statement breaks a db_query rule. ``str(exc)`` is the one-line message the agent sees."""


class Cell(NamedTuple):
    """One value of a result row, as the server sent it.

    ``text`` is the value as text (None for NULL), cut by the server to a bounded window; ``length`` is the
    value's full length in characters, so a cell the server cut is known to be cut.
    """

    text: str | None
    length: int | None


# -- the statement's shape -------------------------------------------------------------------------------------------

_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$\x80-￿]")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\x80-￿][A-Za-z0-9_\x80-￿]*)?\$")
_FIRST_WORD = re.compile(r"(?i)(?:select|with)\b")


def _unclosed(what: str) -> QueryRefused:
    return QueryRefused(f"{_RULE} A {what} is not closed.")


def _skeleton(sql: str) -> str:
    """``sql`` with every comment blanked and every quoted string, quoted name and dollar-quoted body masked,
    character for character, so positions still match ``sql``. Raises on one that is not closed."""
    out = list(sql)
    n = len(sql)

    def fill(start: int, end: int, ch: str) -> None:
        out[start:end] = ch * (end - start)

    i = 0
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            fill(i, j, " ")
            i = j
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:  # PostgreSQL nests block comments
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise _unclosed("/* comment")
            fill(i, j, " ")
            i = j
        elif c in "'\"":
            # E'...' is an escape string, where a backslash escapes the next character (a quote included)
            escapes = c == "'" and i > 0 and sql[i - 1] in "eE" and (i < 2 or not _IDENT_CHAR.match(sql[i - 2]))
            j = i + 1
            while True:
                if j >= n:
                    raise _unclosed("quoted string" if c == "'" else "quoted name")
                if escapes and sql[j] == "\\":
                    j += 2
                elif sql[j] == c and j + 1 < n and sql[j + 1] == c:
                    j += 2
                elif sql[j] == c:
                    break
                else:
                    j += 1
            fill(i, j + 1, "#")
            i = j + 1
        elif c == "$" and (i == 0 or not _IDENT_CHAR.match(sql[i - 1])) and (tag := _DOLLAR_TAG.match(sql, i)):
            j = sql.find(tag.group(0), tag.end())
            if j < 0:
                raise _unclosed("dollar-quoted string")
            end = j + len(tag.group(0))
            fill(i, end, "#")
            i = end
        else:
            i += 1
    return "".join(out)


def check_query_shape(sql: str) -> str:
    """The statement to run: ``sql`` without surrounding whitespace and without one trailing ``;``.

    Raises ``QueryRefused`` unless ``sql`` is one statement that starts with SELECT or WITH (after any comments),
    with every quote and comment closed and its parentheses balanced. This is a courtesy check that explains a
    refusal; the database is the authority. It refuses a second statement too, and PostgreSQL refuses one again,
    since the statement is prepared. A WITH-prefixed write (``WITH x AS (...) DELETE ...``) passes here and is
    the server's to refuse: the login is read-only and holds no write privilege.
    """
    if not isinstance(sql, str):
        raise QueryRefused(f"{_RULE} Pass the statement as text.")
    if len(sql) > MAX_SQL_CHARS:
        raise QueryRefused(f"{_RULE} This one is {len(sql)} characters long; the limit is {MAX_SQL_CHARS}.")
    if "\x00" in sql:
        raise QueryRefused(f"{_RULE} It may not contain a NUL character.")
    skeleton = _skeleton(sql)
    end = skeleton.find(";")
    if end >= 0:
        if skeleton[end + 1 :].strip():
            raise QueryRefused(f"{_RULE} Send one statement only: something follows the ';'.")
        sql, skeleton = sql[:end], skeleton[:end]
    head = skeleton.lstrip()
    if not _FIRST_WORD.match(head):
        first = head.split(None, 1)[0][:20] if head else ""
        raise QueryRefused(f"{_RULE} This one starts with {first!r}." if first else f"{_RULE} This one is empty.")
    depth = 0
    for ch in skeleton:
        depth += (ch == "(") - (ch == ")")
        if depth < 0:
            break
    if depth:
        raise QueryRefused(f"{_RULE} Its parentheses do not balance.")
    return sql.strip()


# -- the result text -------------------------------------------------------------------------------------------------

# Characters that would start a new line (or read as one) in the result text: escaped so a value cannot forge a row.
_LINE_BREAKING = re.compile(r"[\x00-\x08\x0a-\x1f\x7f\x85  ]")
_FOOTER_RESERVE = 200


def _escape(text: str) -> str:
    def one(match: re.Match[str]) -> str:
        ch = match.group(0)
        if ch == "\n":
            return "\\n"
        if ch == "\r":
            return "\\r"
        return f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}"

    return _LINE_BREAKING.sub(one, text)


def _render(cell: Cell) -> str:
    """A cell as shown: redacted first, then clipped to ``MAX_CELL_CHARS``, so a secret the clip would split is
    masked whole. A cell the server cut also loses its last scan-pad characters after redaction: a secret split
    by the server's cut matches no pattern, and masking earlier in the window pulls it toward the front."""
    if cell.text is None:
        return "NULL"
    total = cell.length if cell.length is not None else len(cell.text)
    cut = total > len(cell.text)
    shown = redact_output(cell.text)
    if cut:
        shown = shown[: max(0, len(shown) - redaction_scan_pad())]
    shown = _escape(shown)
    if not cut and len(shown) <= MAX_CELL_CHARS:
        return shown
    kept = shown[:MAX_CELL_CHARS]
    return f"{kept}…[+{max(total - len(kept), 1)} chars]"


def format_result(columns: Sequence[str], rows: Sequence[Sequence[Cell]], *, truncated: bool) -> str:
    """Rows as compact text: a header of column names, one line per row (cells joined by `` | ``), then a line
    with the row count. ``truncated`` says the query had more than ``MAX_ROWS`` rows. The whole text stays under
    ``MAX_OUTPUT_CHARS``: rows past that are left out, and the last line says so."""
    header = " | ".join(_escape(redact_output(name)) for name in columns) if columns else "(no columns)"
    lines = [header]
    used = len(header) + 1
    for row in rows:
        line = " | ".join(_render(cell) for cell in row)
        if used + len(line) + 1 > MAX_OUTPUT_CHARS - _FOOTER_RESERVE:
            lines.append(
                f"(output cut at {MAX_OUTPUT_CHARS} characters after {len(lines) - 1} of {len(rows)} rows; "
                "select fewer columns or rows)"
            )
            return "\n".join(lines)
        lines.append(line)
        used += len(line) + 1
    if truncated:
        lines.append(f"(first {MAX_ROWS} rows shown; the query returned more. Narrow it with WHERE or LIMIT.)")
    else:
        lines.append(f"({len(rows)} row{'' if len(rows) == 1 else 's'})")
    return "\n".join(lines)
