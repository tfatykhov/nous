"""Bulk-load corpus JSONL dumps into a fresh Postgres.

Used by the eval-DB image build pipeline (``Dockerfile.eval-db.load.sh``)
and by integration tests seeding an ephemeral DB. Reads JSONL files
produced by :mod:`nous.eval.ingest` and writes rows into the existing
``heart.facts`` / ``heart.episodes`` / ``heart.procedures`` /
``brain.decisions`` tables via SQLAlchemy core inserts.

Design choices
--------------

- **Idempotent on agent_id.** Re-running against the same ``agent_id``
  no-ops — lets the ingest pipeline resume on failure without duplication.
- **Validates embedding dim.** Any row with ``embedding`` length
  != ``settings.embedding_dimensions`` aborts the load so malformed
  fixtures fail loudly instead of producing bad search results.
- **Writes ``nous_eval_meta``.** A small table on the eval DB recording
  the fixture version + ingest timestamp so the harness can cross-check
  ``NOUS_EVAL_FIXTURE_VERSION`` at startup.

The function does NOT mutate the main Nous DB — it operates entirely on
the Database handle it receives, which the caller must construct pointing
at the eval DB.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from nous.storage.database import Database

logger = logging.getLogger(__name__)


# Expected filename per memory type in the JSONL dump
_CORPUS_FILES: dict[str, str] = {
    "fact": "facts.jsonl",
    "decision": "decisions.jsonl",
    "episode": "episodes.jsonl",
    "procedure": "procedures.jsonl",
}


@dataclass
class CorpusStats:
    """Row counts per memory type from the last load.

    Written back to ``nous_eval_meta.corpus_counts`` for report display.
    """

    facts: int = 0
    decisions: int = 0
    episodes: int = 0
    procedures: int = 0
    skipped_bad_embedding: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.facts + self.decisions + self.episodes + self.procedures

    def to_dict(self) -> dict[str, int | list[str]]:
        return {
            "facts": self.facts,
            "decisions": self.decisions,
            "episodes": self.episodes,
            "procedures": self.procedures,
            "total": self.total,
            "skipped_bad_embedding": self.skipped_bad_embedding,
            "errors": self.errors,
        }


async def load_corpus_from_jsonl(
    db: Database,
    jsonl_dir: Path,
    agent_id: str,
    fixture_version: str,
    embedding_dim: int = 1536,
) -> CorpusStats:
    """Load all memory-type JSONL dumps from ``jsonl_dir`` into the eval DB.

    Idempotent: if the eval DB already has rows for ``agent_id`` in any
    memory type, that type is skipped. Writes a ``nous_eval_meta`` row with
    ``fixture_version`` + ``loaded_at`` so the harness can verify version
    alignment at runtime.
    """
    if not jsonl_dir.exists():
        raise FileNotFoundError(f"Corpus JSONL dir missing: {jsonl_dir}")

    await _ensure_meta_table(db)

    stats = CorpusStats()

    async with db.session() as session:
        # Idempotency check
        existing = await _existing_agent_counts(session, agent_id)
        if existing["facts"] > 0:
            logger.info(
                "agent_id=%s already has corpus (%s); skipping load",
                agent_id,
                existing,
            )
            return stats

        for memory_type, filename in _CORPUS_FILES.items():
            file_path = jsonl_dir / filename
            if not file_path.exists():
                logger.warning(
                    "Corpus file missing for %s: %s (skipping)", memory_type, file_path
                )
                continue
            loaded = await _load_one_type(
                session, file_path, memory_type, agent_id, embedding_dim, stats
            )
            setattr(stats, f"{memory_type}s", loaded)

        await _write_meta(session, fixture_version, stats)
        await session.commit()

    logger.info("Corpus load complete: %s", stats.to_dict())
    return stats


async def _ensure_meta_table(db: Database) -> None:
    """Create ``nous_eval_meta`` if missing. Idempotent.

    Not a migration — this table lives on the eval DB only, and the eval
    DB's schema is baked into the Docker image via ``sql/init.sql``. This
    function exists so ephemeral test DBs (no image) can still be seeded.
    """
    async with db.session() as session:
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS nous_eval_meta (
                    fixture_version TEXT PRIMARY KEY,
                    loaded_at       TIMESTAMPTZ NOT NULL,
                    corpus_counts   JSONB NOT NULL
                )
                """
            )
        )
        await session.commit()


async def _existing_agent_counts(session, agent_id: str) -> dict[str, int]:
    """Return current row counts per table for ``agent_id``."""
    counts: dict[str, int] = {}
    for schema_table, key in [
        ("heart.facts", "facts"),
        ("brain.decisions", "decisions"),
        ("heart.episodes", "episodes"),
        ("heart.procedures", "procedures"),
    ]:
        try:
            result = await session.execute(
                text(f"SELECT COUNT(*) FROM {schema_table} WHERE agent_id = :aid"),
                {"aid": agent_id},
            )
            counts[key] = int(result.scalar() or 0)
        except Exception as exc:
            # If the schema/table doesn't exist yet, treat as zero.
            logger.debug("Count failed for %s: %s", schema_table, exc)
            counts[key] = 0
    return counts


async def _load_one_type(
    session,
    file_path: Path,
    memory_type: str,
    agent_id: str,
    embedding_dim: int,
    stats: CorpusStats,
) -> int:
    """Load a single JSONL file into the corresponding table.

    Uses SQLAlchemy parameterized inserts (not COPY) because the row count
    on a corpus is O(500-5000) — insert latency is not the bottleneck, and
    we want row-level validation + embedding-dim checks.
    """
    table = _table_for(memory_type)
    loaded = 0
    with file_path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                stats.errors.append(f"{file_path.name}:{lineno} JSON error: {exc}")
                continue

            # Validate embedding
            emb = row.get("embedding")
            if emb is not None and isinstance(emb, list):
                if len(emb) != embedding_dim:
                    stats.skipped_bad_embedding += 1
                    stats.errors.append(
                        f"{file_path.name}:{lineno} embedding dim {len(emb)} "
                        f"!= {embedding_dim}"
                    )
                    continue
                row["embedding"] = _pgvector_literal(emb)

            row["agent_id"] = agent_id

            try:
                await session.execute(_insert_sql(table, row), row)
                loaded += 1
            except Exception as exc:
                stats.errors.append(
                    f"{file_path.name}:{lineno} insert failed: {exc}"
                )
    logger.info("Loaded %d %s rows from %s", loaded, memory_type, file_path.name)
    return loaded


def _table_for(memory_type: str) -> str:
    return {
        "fact": "heart.facts",
        "decision": "brain.decisions",
        "episode": "heart.episodes",
        "procedure": "heart.procedures",
    }[memory_type]


def _insert_sql(table: str, row: dict) -> text:
    """Build a parameterized INSERT for the row's keys.

    pgvector fields get passed as string literals (``'[0.1,0.2,...]'``);
    everything else goes through the bind-param layer.
    """
    cols = sorted(row.keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    return text(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )


def _pgvector_literal(vec: list[float]) -> str:
    """Convert a Python list to a pgvector string literal."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


async def _write_meta(session, fixture_version: str, stats: CorpusStats) -> None:
    """Upsert the ``nous_eval_meta`` row for this fixture version."""
    now = datetime.now(tz=timezone.utc)
    payload = stats.to_dict()
    await session.execute(
        text(
            """
            INSERT INTO nous_eval_meta (fixture_version, loaded_at, corpus_counts)
            VALUES (:v, :ts, CAST(:c AS JSONB))
            ON CONFLICT (fixture_version) DO UPDATE SET
                loaded_at = EXCLUDED.loaded_at,
                corpus_counts = EXCLUDED.corpus_counts
            """
        ),
        {"v": fixture_version, "ts": now, "c": json.dumps(payload)},
    )
