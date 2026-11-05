"""The database a run reads: a fresh clone of a template, or a named scratch database.

A clone (``CREATE DATABASE <name> TEMPLATE <template>``) gives every run the same starting
bytes, and keeps the template untouched: reads write too (working memory, retrieval telemetry,
access counters), and sessions commit per call, so a rolled-back transaction cannot isolate a
run. The template needs no other connection open while it is copied.

Never written: a prod copy (``nous_prod*``) and anything under ``nous_eval_served_`` other than
this run's own clone (templates, other runs' clones). ``--db-name`` refuses those names whatever
other flag is passed, and a run drops only the clone it created, by the exact name it created.
"""

from __future__ import annotations

import re
from typing import Any

SCRATCH_RE = re.compile(r"^(nous_eval|nous_test|beam)")
PROTECTED_RE = re.compile(r"^(nous_prod|nous_eval_served_)")
_IDENT_RE = re.compile(r"^[a-z0-9_]{1,63}$")
CLONE_PREFIX = "nous_eval_served_run_"


def check_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise SystemExit(f"database name {name!r}: lower-case letters, digits and _ only")
    return name


def check_scratch(name: str, *, any_scratch_name: bool = False) -> str:
    """A database a run may write directly: an eval or test database (``any_scratch_name``: any
    name, for a test database not named like one), never a prod copy, a template or a clone."""
    check_ident(name)
    if PROTECTED_RE.match(name):
        raise SystemExit(f"--db-name {name!r}: prod copies and served templates/clones are never run on directly")
    if not any_scratch_name and not SCRATCH_RE.match(name):
        raise SystemExit(
            f"--db-name {name!r} is not a scratch database (nous_eval*, nous_test*, beam*); "
            "use --db-template to run on a clone of it"
        )
    return name


def clone_name(tag: str) -> str:
    return check_ident(CLONE_PREFIX + re.sub(r"[^a-z0-9_]", "_", tag.lower()))[:63]


def _admin_engine(conn: dict[str, Any]):
    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import create_async_engine

    url = URL.create(
        "postgresql+asyncpg",
        username=conn["db_user"],
        password=conn["db_password"],
        host=conn["db_host"],
        port=conn["db_port"],
        database="postgres",
    )
    return create_async_engine(url, isolation_level="AUTOCOMMIT", pool_size=1, max_overflow=0)


async def create_clone(conn: dict[str, Any], template: str, name: str) -> str:
    """CREATE DATABASE name TEMPLATE template; returns the name, the only one drop_clone takes."""
    from sqlalchemy import text

    check_ident(template)
    check_ident(name)
    if not name.startswith(CLONE_PREFIX):
        raise SystemExit(f"clone {name!r} must start with {CLONE_PREFIX}")
    eng = _admin_engine(conn)
    try:
        async with eng.connect() as c:
            exists = (await c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})).first()
            if exists:
                raise SystemExit(f"database {name} exists; pick another --tag or drop it")
            await c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    finally:
        await eng.dispose()
    return name


async def drop_clone(conn: dict[str, Any], name: str, *, created: str | None) -> None:
    """Drop the clone this run created: ``name`` must be exactly ``created`` (create_clone's return)."""
    from sqlalchemy import text

    if created is None or name != created or not name.startswith(CLONE_PREFIX):
        raise SystemExit(f"refusing to drop {name!r}: not the clone this run created")
    eng = _admin_engine(conn)
    try:
        async with eng.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{check_ident(name)}" WITH (FORCE)'))
    finally:
        await eng.dispose()


async def table_write_counts(db: Any) -> dict[str, int]:
    """Rows inserted + updated + deleted so far, per table (pg_stat_user_tables)."""
    from sqlalchemy import text

    async with db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT schemaname || '.' || relname AS tname, n_tup_ins + n_tup_upd + n_tup_del AS w "
                    "FROM pg_stat_user_tables WHERE schemaname IN ('heart', 'brain', 'nous_system')"
                )
            )
        ).all()
    return {r.tname: int(r.w) for r in rows}


def write_diff(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {t: after[t] - before.get(t, 0) for t in sorted(after) if after[t] != before.get(t, 0)}
