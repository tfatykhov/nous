"""Guard: migration version prefixes must be unique.

The migrator keys nous_system.schema_migrations on the numeric filename
prefix. Two files sharing a prefix either crash a fresh bootstrap (primary
key violation) or silently skip the second file on an existing database.
"""

from collections import Counter
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "sql" / "migrations"


def test_migration_version_prefixes_are_unique():
    versions = [p.stem.split("_", 1)[0] for p in MIGRATIONS.glob("*.sql")]
    dupes = sorted(v for v, n in Counter(versions).items() if n > 1)
    assert not dupes, f"duplicate migration version prefixes: {dupes}"
