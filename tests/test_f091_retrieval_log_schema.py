"""F091: the INSERT in main.py must match migration 070.

Nothing tested this. The writer swallows failures (now ERROR-once, previously
DEBUG), so an unapplied migration or a renamed column surfaced only as an
empty dashboard. This parses both sides and compares them, with no DB needed —
so it runs in CI on any platform.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "sql" / "migrations" / "070_retrieval_telemetry.sql"
MAIN = REPO / "nous" / "main.py"


def _migration_columns() -> list[str]:
    sql = MIGRATION.read_text(encoding="utf-8")
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS nous_system\.retrieval_log\s*\((.*?)\n\);",
        sql, re.S,
    ).group(1)
    cols = []
    for raw in body.split("\n"):
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        name = line.split()[0]
        if name.upper() in {"PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN"}:
            continue
        cols.append(name)
    return cols


def _insert_columns_and_params() -> tuple[list[str], list[str]]:
    src = MAIN.read_text(encoding="utf-8")
    stmt = re.search(
        r'"INSERT INTO nous_system\.retrieval_log "(.*?)\), \{', src, re.S,
    ).group(1)
    joined = "".join(re.findall(r'"([^"]*)"', stmt))
    cols_blob = re.search(r"\((.*?)\)\s*VALUES", joined, re.S).group(1)
    vals_blob = re.search(r"VALUES\s*\((.*)", joined, re.S).group(1)
    cols = [c.strip() for c in cols_blob.split(",") if c.strip()]
    params = [p.strip().lstrip(":").rstrip(")")
              for p in vals_blob.split(",") if p.strip()]
    return cols, params


def test_every_inserted_column_exists_in_the_migration():
    missing = set(_insert_columns_and_params()[0]) - set(_migration_columns())
    assert not missing, f"INSERT writes columns not in migration 070: {sorted(missing)}"


def test_column_and_parameter_counts_line_up():
    cols, params = _insert_columns_and_params()
    assert len(cols) == len(params), (
        f"{len(cols)} columns vs {len(params)} bind params — a positional "
        f"mismatch would write values into the wrong columns"
    )


def test_every_bind_param_is_supplied_by_the_writer():
    src = MAIN.read_text(encoding="utf-8")
    supplied = set(re.findall(r'^\s*"(\w+)":', src, re.M))
    missing = [p for p in _insert_columns_and_params()[1] if p not in supplied]
    assert not missing, f"bind params with no value in the writer dict: {missing}"


def test_agent_id_is_present_and_not_null():
    """Project convention: every table is agent-scoped."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert re.search(r"agent_id\s+TEXT\s+NOT NULL", sql)


def test_no_semicolon_inside_sql_line_comments():
    """A `;` inside a `--` comment splits the statement in the migrator."""
    for i, line in enumerate(MIGRATION.read_text(encoding="utf-8").splitlines(), 1):
        if "--" in line and ";" in line.split("--", 1)[1]:
            raise AssertionError(f"semicolon inside a comment at line {i}: {line!r}")


def test_candidates_is_nullable_so_unsampled_is_distinguishable():
    """NULL means 'not sampled'; '[]' would mean 'sampled, found nothing'."""
    sql = MIGRATION.read_text(encoding="utf-8")
    col = re.search(r"^\s*candidates\s+JSONB(.*)$", sql, re.M).group(1)
    assert "NOT NULL" not in col.upper()
    assert "DEFAULT" not in col.upper()
