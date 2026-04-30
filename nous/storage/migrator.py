"""Auto-migration runner — applies pending SQL migrations on startup.

Discovers sql/migrations/*.sql files, tracks applied versions in
nous_system.schema_migrations, and executes pending ones in order.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "sql" / "migrations"

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS nous_system.schema_migrations (
    version    VARCHAR(20) PRIMARY KEY,
    name       VARCHAR(255) NOT NULL,
    checksum   VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT now()
);
"""


def _split_sql_statements(sql: str) -> list[str]:
    """Split a migration file into individual SQL statements.

    Strips `-- ...` line comments BEFORE walking the body, then splits on
    top-level `;` while tracking single-quoted string literals so a `;`
    inside a string (e.g. a COMMENT body) does not chop the statement.

    Bugs prevented:
    - Migration 034 had "-- legacy rows; backfill..." that was mis-split
      into "backfill..." and executed as SQL (fixed in 38531ba).
    - Migration 039 had "COMMENT ... 'foo; bar'" which the naive splitter
      cut mid-string, raising PostgresSyntaxError on prod boot.

    Single-quote escapes are handled the SQL way: doubled `''` inside a
    string. Block comments `/* ... */` and dollar-quoted strings `$$...$$`
    are NOT supported — current migrations don't use them.
    """
    stripped = "\n".join(
        ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")
    )
    stmts: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    n = len(stripped)
    while i < n:
        ch = stripped[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                # SQL escapes single-quote by doubling: '' inside a string.
                if i + 1 < n and stripped[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


async def run_migrations(engine: AsyncEngine) -> list[str]:
    """Apply pending SQL migrations and return list of newly applied names."""
    if not _MIGRATIONS_DIR.is_dir():
        logger.debug("No migrations directory found at %s", _MIGRATIONS_DIR)
        return []

    # Discover migration files sorted by name (e.g. 006_event_bus.sql)
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        return []

    applied: list[str] = []
    async with engine.begin() as conn:
        # Self-bootstrap: create tracking table if it doesn't exist
        await conn.execute(text(_BOOTSTRAP_SQL))

        # Get already-applied versions
        result = await conn.execute(
            text("SELECT version FROM nous_system.schema_migrations")
        )
        existing = {row[0] for row in result}

        for path in files:
            # Extract version from filename prefix (e.g. "006" from "006_event_bus.sql")
            version = path.stem.split("_", 1)[0]
            if version in existing:
                continue

            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()

            logger.info("Applying migration %s ...", path.name)
            # asyncpg doesn't support multiple statements in one execute(),
            # so split and run each statement individually.
            for stmt in _split_sql_statements(sql):
                await conn.execute(text(stmt))
            await conn.execute(
                text(
                    "INSERT INTO nous_system.schema_migrations (version, name, checksum) "
                    "VALUES (:version, :name, :checksum)"
                ),
                {"version": version, "name": path.stem, "checksum": checksum},
            )
            applied.append(path.stem)
            logger.info("Migration %s applied", path.name)

    if applied:
        logger.info("Migrations applied: %s", applied)
    else:
        logger.debug("All migrations up to date")

    return applied
