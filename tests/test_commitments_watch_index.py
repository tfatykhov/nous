"""F111 PR B1: migration 108, the overdue watcher's index (plan §2, §9 DDL test).

The migration file is the owner's to add (a headless job may not write under sql/migrations/), so its DDL lives in
the F111 doc's appendix. These tests read it from there: the file, once added, must be that block exactly; the block
applies twice cleanly and builds the index the watcher's read uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from nous.storage.migrator import _split_sql_statements

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_108 = ROOT / "sql" / "migrations" / "108_commitments_watch_index.sql"
DOC = ROOT / "docs" / "features" / "F111-obligation-ledger.md"


def _ddl() -> str:
    doc = DOC.read_text(encoding="utf-8")
    appendix = doc.split("## Appendix: the DDL of migration 108", 1)[1]
    return appendix.split("```sql\n", 1)[1].split("```", 1)[0]


def test_the_ddl_has_no_semicolon_in_a_comment_and_no_grant():
    ddl = _ddl()
    comments = [ln for ln in ddl.splitlines() if ln.lstrip().startswith("--")]
    assert comments and not [ln for ln in comments if ";" in ln]
    statements = _split_sql_statements(ddl)
    assert len(statements) == 2 and not [s for s in statements if "GRANT" in s.upper()]


def test_the_migration_file_once_added_is_the_ddl_the_doc_records():
    if not MIGRATION_108.exists():
        pytest.skip("migration 108 is the owner's to add (see the PR); the doc's appendix is the DDL")
    assert MIGRATION_108.read_text(encoding="utf-8") == _ddl()


@pytest.mark.postgres_only
async def test_the_ddl_applies_twice_and_builds_the_watchers_index(db):
    for _ in range(2):
        async with db.engine.begin() as conn:
            for statement in _split_sql_statements(_ddl()):
                await conn.execute(text(statement))
    async with db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'brain' AND tablename = 'commitments' "
                    "AND indexname = 'commitments_watch_idx'"
                )
            )
        ).all()
    [(indexdef,)] = rows
    assert "(agent_id, status, due_at)" in indexdef
    assert "WHERE (status = ANY (ARRAY['open'::text, 'proposed'::text]))" in indexdef
