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
    from nous.storage.database import Database


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


async def assert_eval_db_schema_matches_orm(db: "Database") -> None:
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
