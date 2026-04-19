"""Unit tests for nous.storage.migrator._split_sql_statements.

Regression coverage for the bug where a semicolon inside a `-- ...` line
comment broke the statement splitter and leaked comment prose into an
executable SQL statement (migration 034 / F047).
"""

from __future__ import annotations

from nous.storage.migrator import _split_sql_statements


def test_split_simple_statements():
    sql = "CREATE TABLE t (id INT); CREATE INDEX idx ON t(id);"
    assert _split_sql_statements(sql) == [
        "CREATE TABLE t (id INT)",
        "CREATE INDEX idx ON t(id)",
    ]


def test_split_strips_leading_line_comments():
    sql = """
-- A comment.
CREATE TABLE t (id INT);
-- Another comment.
CREATE INDEX idx ON t(id);
"""
    stmts = _split_sql_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE")
    assert stmts[1].startswith("CREATE INDEX")


def test_split_handles_semicolon_inside_line_comment():
    """Regression for migration 034: semicolon inside a -- comment must
    not cause the splitter to leak the tail of the comment into the next
    statement as bogus SQL."""
    sql = """
-- NULL = not yet classified (legacy rows; backfill handler will populate).
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS actionable BOOLEAN DEFAULT NULL;
"""
    stmts = _split_sql_statements(sql)
    assert len(stmts) == 1
    assert stmts[0].startswith("ALTER TABLE")
    # The comment tail must NOT have leaked into executable SQL.
    for stmt in stmts:
        assert "backfill handler" not in stmt
        assert "legacy rows" not in stmt


def test_split_handles_indented_comments():
    sql = """
ALTER TABLE t
    ADD COLUMN x INT,
    -- inline indented comment; with a semicolon
    ADD COLUMN y INT;
"""
    stmts = _split_sql_statements(sql)
    assert len(stmts) == 1
    assert "ADD COLUMN x INT" in stmts[0]
    assert "ADD COLUMN y INT" in stmts[0]
    assert "inline indented" not in stmts[0]


def test_split_preserves_comment_on_column_strings():
    """`COMMENT ON COLUMN ... IS '...'` is valid SQL — it must survive
    splitting (the string literal is not a -- comment)."""
    sql = """
COMMENT ON COLUMN heart.facts.actionable IS
    'F047: True=pending action, False=observation/resolved, NULL=unclassified';
"""
    stmts = _split_sql_statements(sql)
    assert len(stmts) == 1
    assert stmts[0].startswith("COMMENT ON COLUMN")
    assert "F047" in stmts[0]


def test_split_drops_empty_chunks():
    assert _split_sql_statements(";;;") == []
    assert _split_sql_statements("   ;   ;  ") == []
    assert _split_sql_statements("") == []
    assert _split_sql_statements("\n\n-- just a comment\n\n") == []


def test_split_full_migration_034():
    """End-to-end: feed the exact content of migration 034 and assert
    that the three expected statements come out (ALTER TABLE, CREATE
    INDEX, COMMENT ON COLUMN × 2). The ORIGINAL buggy version of this
    file would produce a 4th bogus statement starting with "backfill"."""
    sql = """-- F047: Actionability classification at learn time.
-- Adds two nullable columns and a partial index on heart.facts.
-- NULL = not yet classified (legacy rows; backfill handler will populate).
-- Partial index optimizes the "find actionable facts" query used by heartbeat.

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS actionable BOOLEAN DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS actionable_confidence REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_facts_actionable_agent
    ON heart.facts(agent_id, actionable)
    WHERE actionable = TRUE;

COMMENT ON COLUMN heart.facts.actionable IS
    'F047: True=pending action, False=observation/resolved, NULL=unclassified';
COMMENT ON COLUMN heart.facts.actionable_confidence IS
    'F047: Classifier confidence 0.0-1.0';
"""
    stmts = _split_sql_statements(sql)
    # Exactly 4: ALTER + CREATE INDEX + 2× COMMENT ON COLUMN.
    assert len(stmts) == 4, f"expected 4 statements, got {len(stmts)}: {stmts}"
    assert stmts[0].startswith("ALTER TABLE")
    assert stmts[1].startswith("CREATE INDEX")
    assert stmts[2].startswith("COMMENT ON COLUMN")
    assert stmts[3].startswith("COMMENT ON COLUMN")
    # No bogus chunk starts with "backfill".
    assert not any(s.lower().startswith("backfill") for s in stmts)
