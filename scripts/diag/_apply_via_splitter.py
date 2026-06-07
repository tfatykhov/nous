"""Apply given migration files THROUGH the real migrator splitter, to validate that
nous/storage/migrator.py can actually parse+run them (psql tolerates DO blocks the
splitter cannot). Connects via DB_* env. Usage: python _apply_via_splitter.py f1.sql f2.sql
"""
import asyncio
import sys

from sqlalchemy import text

from nous.config import Settings
from nous.storage.database import Database
from nous.storage.migrator import _split_sql_statements


async def main() -> int:
    db = Database(Settings())
    await db.connect()
    async with db.engine.begin() as conn:  # mirror run_migrations: one tx per file-set
        for path in sys.argv[1:]:
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            stmts = _split_sql_statements(sql)
            print(f"{path}: {len(stmts)} statement(s)")
            for stmt in stmts:
                await conn.execute(text(stmt))
            print(f"  applied OK")
    await db.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
