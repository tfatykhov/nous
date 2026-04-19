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

    Strips `-- ...` line comments BEFORE splitting on `;` so a semicolon
    inside a comment does not break the splitter (see bug: migration 034
    had "-- legacy rows; backfill..." that was mis-split into "backfill..."
    and executed as SQL).

    Block comments `/* ... */` are NOT handled — our migrations don't use
    them. Dollar-quoted strings `$$...$$` would also need care; current
    migrations don't use those either.
    """
    stripped = "\n".join(
        ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")
    )
    return [stmt.strip() for stmt in stripped.split(";") if stmt.strip()]


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
