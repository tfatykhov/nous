"""Pre-flight schema check: assert eval DB matches what the ORM expects.

When the eval DB is missing a recent migration (e.g., a new column added
to ``heart.episodes``), the SQLAlchemy ORM will issue a SELECT that
includes the new column, asyncpg will raise ``UndefinedColumnError``,
and that error poisons the connection's transaction (asyncpg marks it
ABORTED). All subsequent queries in the same session fail with
``InFailedSQLTransactionError``.

PR #398 fixed the cascade behavior in ``Heart._recall``, but the *root
cause* is still silent: the eval reports something like "0% sufficient"
without ever telling you that retrieval crashed at the schema level.

This pre-flight introspects the ORM model columns the harness depends
on, queries ``information_schema.columns`` against the eval DB, and
raises ``EvalDBSchemaDriftError`` early — with a one-step remediation
hint — if any ORM-required column is missing. Would have surfaced
today's missing-migration-040 gap in seconds rather than hours of
investigation.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from nous.storage.models import (
    Censor,
    Decision,
    Episode,
    Fact,
    Procedure,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

    from nous.storage.database import Database

logger = logging.getLogger(__name__)


class EvalDBSchemaDriftError(RuntimeError):
    """Raised when the eval DB is missing ORM-required columns.

    The message includes which tables/columns are missing and a one-line
    command to apply pending migrations.
    """


# Tables the retrieval pipeline actually reads from. Adding more models
# here is cheap and makes the check stricter; removing them weakens it.
_REQUIRED_MODELS: tuple[type, ...] = (
    Episode,
    Fact,
    Procedure,
    Censor,
    Decision,
)


def _orm_column_names(model: type) -> set[str]:
    """Return the column names the ORM will SELECT for this model."""
    return {col.name for col in model.__table__.columns}


def _qualified_table(model: type) -> tuple[str, str]:
    """Return ``(schema, table)`` for an ORM model."""
    table = model.__table__
    schema = table.schema or "public"
    return schema, table.name


async def assert_eval_db_schema_matches_orm(db: Database) -> None:
    """Raise ``EvalDBSchemaDriftError`` if the eval DB lacks ORM columns.

    Runs one ``information_schema.columns`` query per model (cheap;
    typically 5 round-trips at startup). On success returns silently.

    Call once at eval startup, before any ``heart.recall`` invocation.
    """
    missing: list[tuple[str, list[str]]] = []

    async with db.session() as session:
        for model in _REQUIRED_MODELS:
            schema, table = _qualified_table(model)
            orm_cols = _orm_column_names(model)

            result = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table"
                ),
                {"schema": schema, "table": table},
            )
            db_cols = {row[0] for row in result}

            # ``search_tsv`` is DB-generated and excluded from inserts in
            # the snapshot script, but the ORM doesn't model it either —
            # so we don't need a special case here.
            missing_cols = sorted(orm_cols - db_cols)
            if missing_cols:
                missing.append((f"{schema}.{table}", missing_cols))

    if missing:
        raise EvalDBSchemaDriftError(_format_drift_message(missing))


def _format_drift_message(missing: list[tuple[str, list[str]]]) -> str:
    lines = [
        "Eval DB schema drift detected — ORM-required columns missing:",
        "",
    ]
    for table, cols in missing:
        lines.append(f"  {table}: {', '.join(cols)}")
    lines += [
        "",
        "This usually means the eval DB image was built before one or",
        "more recent migrations. The canonical fix is to rebuild the",
        "eval DB volume so initdb re-applies every migration:",
        "",
        "  uv run python -m nous_eval.rebuild",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Baked-image migration history
# ---------------------------------------------------------------------------
#
# Dockerfile.eval-db applies init.sql + every sql/migrations/*.sql through
# docker-entrypoint-initdb.d, which executes the files but never records them
# in nous_system.schema_migrations. Handing such a DB to run_migrations makes
# it re-run every migration, and the first non-idempotent statement (e.g. the
# bare CREATE UNIQUE INDEX in 022) aborts with a duplicate-relation error
# before any migration the image actually lacks is reached.
#
# initdb runs the files in lexicographic order, so the image holds a
# contiguous PREFIX of the migrations. The boundary is the newest migration
# whose tables / added columns / indexes all exist; everything up to it is
# recorded as applied and run_migrations then applies only the tail. Replaying
# migrations with per-statement error tolerance was rejected: it would re-run
# data UPDATEs against the fixture corpus.

_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\.(\w+)",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\b",
    re.IGNORECASE,
)
_ALTER_TABLE_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(\w+)\.(\w+)",
    re.IGNORECASE,
)
_ADD_COLUMN_RE = re.compile(r"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)

Artifact = tuple[str, ...]


def migration_artifacts(sql: str) -> list[Artifact]:
    """Return the schema objects a migration creates, as probe-able tuples.

    ``("table", schema, name)``, ``("column", schema, table, column)`` and
    ``("index", name)``. Only schema-qualified tables are captured; anything
    this cannot parse is simply not probed.
    """
    from nous.storage.migrator import _split_sql_statements

    artifacts: list[Artifact] = []
    for stmt in _split_sql_statements(sql):
        if m := _CREATE_TABLE_RE.match(stmt):
            artifacts.append(("table", m.group(1).lower(), m.group(2).lower()))
        elif m := _CREATE_INDEX_RE.match(stmt):
            artifacts.append(("index", m.group(1).lower()))
        elif m := _ALTER_TABLE_RE.match(stmt):
            schema, table = m.group(1).lower(), m.group(2).lower()
            for col in _ADD_COLUMN_RE.findall(stmt):
                artifacts.append(("column", schema, table, col.lower()))
    return artifacts


async def _artifact_exists(conn: AsyncConnection, artifact: Artifact) -> bool:
    kind = artifact[0]
    if kind == "table":
        query = "SELECT 1 FROM information_schema.tables WHERE table_schema = :schema AND table_name = :name"
        params = {"schema": artifact[1], "name": artifact[2]}
    elif kind == "column":
        query = (
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
        )
        params = {"schema": artifact[1], "table": artifact[2], "column": artifact[3]}
    else:
        query = "SELECT 1 FROM pg_class WHERE relname = :name AND relkind IN ('i', 'I')"
        params = {"name": artifact[1]}
    return (await conn.execute(text(query), params)).first() is not None


async def seed_baked_migration_history(engine: AsyncEngine, migrations_dir: Path | None = None) -> list[str]:
    """Record the migrations a baked eval image already contains.

    No-op unless ``nous_system.schema_migrations`` is empty AND the schema is
    already populated (``brain.decisions`` exists) — i.e. the DB was built by
    initdb, not by the migrator. Returns the migration names recorded, so the
    following ``run_migrations`` applies only what the image lacks.
    """
    from nous.storage.migrator import _BOOTSTRAP_SQL, _MIGRATIONS_DIR

    files = sorted((migrations_dir or _MIGRATIONS_DIR).glob("*.sql"))
    if not files:
        return []

    async with engine.begin() as conn:
        await conn.execute(text(_BOOTSTRAP_SQL))
        if (await conn.execute(text("SELECT 1 FROM nous_system.schema_migrations LIMIT 1"))).first():
            return []
        if (await conn.execute(text("SELECT to_regclass('brain.decisions')"))).scalar() is None:
            return []

        boundary = -1
        for idx in range(len(files) - 1, -1, -1):
            artifacts = migration_artifacts(files[idx].read_text(encoding="utf-8"))
            if not artifacts:
                continue
            present = True
            for artifact in artifacts:
                if not await _artifact_exists(conn, artifact):
                    present = False
                    break
            if present:
                boundary = idx
                break

        seeded: list[str] = []
        for path in files[: boundary + 1]:
            sql = path.read_text(encoding="utf-8")
            await conn.execute(
                text(
                    "INSERT INTO nous_system.schema_migrations (version, name, checksum) "
                    "VALUES (:version, :name, :checksum)"
                ),
                {
                    "version": path.stem.split("_", 1)[0],
                    "name": path.stem,
                    "checksum": hashlib.sha256(sql.encode()).hexdigest(),
                },
            )
            seeded.append(path.stem)

    if seeded:
        logger.warning(
            "Eval DB had no migration history but a populated schema (baked image); "
            "recorded %d migrations through %s as already applied",
            len(seeded),
            seeded[-1],
        )
    return seeded
