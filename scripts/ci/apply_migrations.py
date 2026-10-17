#!/usr/bin/env python3
"""Apply SQL migrations with the application's own migrator, as the database login in DB_* (the same variables
the application reads), optionally from another directory or only up to a version.

    python scripts/ci/apply_migrations.py --max-version 88      # the history up to 088, as whoever DB_USER is
    python scripts/ci/apply_migrations.py                       # everything pending, as DB_USER
    python scripts/ci/apply_migrations.py --dir tests/fixtures/migration_probe/ok

CI uses it to run new migrations under the application role. Prints the migrations applied; exits 1 on a
failure, with the database error on stderr.
A local run needs PYTHONPATH=. (CI installs the package with pip install -e).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

import nous.storage.migrator as migrator
from nous.config import Settings


def _select(source: Path, max_version: int | None, into: Path) -> None:
    for path in sorted(source.glob("*.sql")):
        if max_version is None or int(path.stem.split("_", 1)[0]) <= max_version:
            shutil.copy(path, into / path.name)


async def _run(directory: Path, max_version: int | None) -> list[str]:
    engine = create_async_engine(Settings().db_url)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _select(directory, max_version, Path(tmp))
            migrator._MIGRATIONS_DIR = Path(tmp)
            return await migrator.run_migrations(engine)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=str(migrator._MIGRATIONS_DIR), help="directory of NNN_name.sql files")
    parser.add_argument("--max-version", type=int, default=None, help="skip migrations numbered above this")
    args = parser.parse_args(argv)
    try:
        applied = asyncio.run(_run(Path(args.dir), args.max_version))
    except Exception as exc:  # the database error is the message
        print(f"apply_migrations: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"apply_migrations: applied {applied}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
