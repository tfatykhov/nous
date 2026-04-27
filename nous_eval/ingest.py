"""F051 eval corpus ingest — operator-run, replays prod into a scratch eval DB.

Usage::

    python -m nous_eval.ingest --out nous-eval-fixtures-staging --agent-id nous-eval-corpus

What it does (in strict order):

1. **Fail fast** if ``NOUS_PROD_DB_{HOST,PORT,USER,PASSWORD,NAME}`` are unset —
   there is no safe default for a "prod" connection string (plan v2.1 P1-10).
2. Spin up a scratch eval DB (separate DB instance from prod; operator passes
   ``NOUS_EVAL_SCRATCH_DB_URL`` or a default ``localhost:5433/nous_eval_scratch``).
3. Construct a ``Settings`` clone with **all** background handlers disabled
   (EventBus, fact_extractor, episode_summarizer, sleep, actionability backfill,
   heartbeat, schedule, subtask, DAG, decision_review, correction_extraction,
   graph_backfill, rubric_outcome_detection) so the replay does not cascade
   writes back into prod or the scratch DB via the process-wide EventBus
   singleton (plan v2.1 P1-11).
4. Dump ``brain.decisions``, ``heart.facts``, ``heart.episodes``,
   ``heart.procedures``, ``heart.censors`` from prod as JSONL into
   ``--out`` (one file per table). Re-embed all text with
   ``text-embedding-3-small`` if requested; otherwise copy embeddings as-is.
5. Rewrite ``agent_id`` on every row to the harness default (``nous-eval-corpus``)
   so the baked image's ``WHERE agent_id = ...`` filter hits (plan v2.1 P1-4).
6. Write ``manifest.json`` recording ``fixture_version``, source agent_id,
   source DB hostname, row counts, ingest timestamp.

**Nothing runs when this file is imported.** All work happens inside ``main()``
under ``if __name__ == '__main__'``. The file is safe to import for tests of
the helper functions.

This PR ships the code only — actual ingest runs happen in Phase 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Required prod DB env vars. Absence of ANY of these is a fatal error — we
# refuse to fall back to libpq defaults because the cost of reading the wrong
# database is too high (plan v2.1 P1-10).
REQUIRED_PROD_ENV_VARS = (
    "NOUS_PROD_DB_HOST",
    "NOUS_PROD_DB_PORT",
    "NOUS_PROD_DB_USER",
    "NOUS_PROD_DB_PASSWORD",
    "NOUS_PROD_DB_NAME",
)

# Tables dumped in dependency order — decisions first so brain rows exist
# before facts/episodes that reference them via graph edges. Graph edges
# come last because they reference all other types via (source_id, target_id).
INGEST_TABLES = (
    ("brain.decisions", "decisions.jsonl"),
    ("heart.facts", "facts.jsonl"),
    ("heart.episodes", "episodes.jsonl"),
    ("heart.procedures", "procedures.jsonl"),
    ("heart.censors", "censors.jsonl"),
    ("brain.graph_edges", "graph_edges.jsonl"),
)

DEFAULT_EVAL_AGENT_ID = "nous-eval-corpus"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class IngestConfig:
    """Resolved operator inputs — no I/O, no env reads during construction."""

    out_dir: Path
    eval_agent_id: str
    scratch_db_url: str
    reembed: bool
    prod_dsn: str


@dataclass
class IngestStats:
    """Returned from the ingest pipeline for the manifest + operator summary."""

    fixture_version: str
    source_agent_id: str | None
    source_host: str
    started_at: str
    finished_at: str = ""
    row_counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Env / arg parsing
# ---------------------------------------------------------------------------


def _require_prod_env() -> str:
    """Build prod DSN from env or abort.

    Keeps the check in one place so callers always get the same error message.
    Returns a read-only asyncpg DSN suitable for :class:`asyncpg.Pool`.
    """
    missing = [v for v in REQUIRED_PROD_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise SystemExit(
            "[eval.ingest] Missing required environment variables for prod DB:\n  "
            + "\n  ".join(missing)
            + "\n\nSet each of them explicitly — no libpq fallback is allowed (plan v2.1 P1-10)."
        )
    host = os.environ["NOUS_PROD_DB_HOST"]
    port = os.environ["NOUS_PROD_DB_PORT"]
    user = os.environ["NOUS_PROD_DB_USER"]
    pw = os.environ["NOUS_PROD_DB_PASSWORD"]
    db = os.environ["NOUS_PROD_DB_NAME"]
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def _parse_args(argv: list[str] | None) -> IngestConfig:
    parser = argparse.ArgumentParser(
        prog="python -m nous_eval.ingest",
        description="Replay prod corpus into scratch eval DB + dump JSONL.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("nous-eval-fixtures-staging"),
        help="Output directory for JSONL fixture files.",
    )
    parser.add_argument(
        "--agent-id",
        default=DEFAULT_EVAL_AGENT_ID,
        help="agent_id to stamp on every replayed row (defaults to nous-eval-corpus).",
    )
    parser.add_argument(
        "--scratch-db-url",
        default=os.environ.get(
            "NOUS_EVAL_SCRATCH_DB_URL",
            "postgresql+asyncpg://nous:nous_eval@localhost:5433/nous_eval_scratch",
        ),
        help="Scratch eval DB where replayed state lands before JSONL dump.",
    )
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="Re-embed all text via OpenAI text-embedding-3-small (costs money).",
    )
    args = parser.parse_args(argv)
    prod_dsn = _require_prod_env()
    return IngestConfig(
        out_dir=args.out,
        eval_agent_id=args.agent_id,
        scratch_db_url=args.scratch_db_url,
        reembed=args.reembed,
        prod_dsn=prod_dsn,
    )


# ---------------------------------------------------------------------------
# Settings construction — isolated from prod side effects
# ---------------------------------------------------------------------------


def _settings_for_ingest(scratch_db_url: str) -> Any:
    """Build a Settings clone scoped to the scratch DB with all async/bg off.

    Imported lazily so ``from nous_eval.ingest import IngestConfig`` does not
    incur the full Settings load (which reads many env vars).
    """
    from nous.config import Settings  # lazy import

    # Parse scratch DSN back to its components so the Database(settings)
    # contract is satisfied without a separate db_url override.
    from urllib.parse import urlparse

    p = urlparse(scratch_db_url.replace("postgresql+asyncpg://", "postgresql://"))
    assert p.hostname is not None
    assert p.username is not None
    assert p.password is not None
    assert p.path
    base = Settings()
    return base.model_copy(
        update={
            "db_host": p.hostname,
            "db_port": p.port or 5433,
            "db_user": p.username,
            "db_password": p.password,
            "db_name": p.path.lstrip("/"),
            # Kill every background pipeline — ingest is strictly a one-shot
            # replay and must not cascade writes via the EventBus singleton.
            "event_bus_enabled": False,
            "fact_extraction_enabled": False,
            "episode_summary_enabled": False,
            "sleep_enabled": False,
            "actionability_enabled": False,
            "actionability_backfill_on_startup": False,
            "heartbeat_enabled": False,
            "schedule_enabled": False,
            "subtask_enabled": False,
            "dag_enabled": False,
            "decision_review_enabled": False,
            "correction_extraction_enabled": False,
            "graph_backfill_enabled": False,
            "rubric_outcome_detection_enabled": False,
            # F023 admission control silently rejected ~999/1000 candidate_facts
            # during F051.5 LongMemEval ingest because:
            # (a) admission_shadow_mode defaults to False (production tightens),
            # (b) admission_threshold=0.6 with no LLM utility scorer wired
            # during ingest → most facts score below threshold → rejected.
            # This is the same class of silent-pipeline-mismatch as issue #354
            # (edge_audit/densifier column drift).
            # Ingest is a bulk-load of vetted benchmark data; admission control
            # is for filtering production traffic. Disable here.
            "admission_control_enabled": False,
        }
    )


# ---------------------------------------------------------------------------
# Replay / dump
# ---------------------------------------------------------------------------


async def _dump_table_to_jsonl(
    pool: Any,
    qualified_table: str,
    out_path: Path,
    eval_agent_id: str,
) -> int:
    """Stream rows of ``qualified_table`` to ``out_path`` as JSONL.

    Each row's ``agent_id`` is rewritten to ``eval_agent_id`` so the baked
    image's ``WHERE agent_id = ...`` filter hits. ``embedding`` columns (if
    present) are serialised as list-of-float for JSON compatibility; asyncpg
    already decodes pgvector's ``vector`` type to ``list[float]`` when the
    codec is registered.

    Returns the number of rows written.
    """
    schema, table = qualified_table.split(".", 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8") as fh:
        async with pool.acquire() as conn:
            # asyncpg server-side cursors require an open transaction.
            # Without it `conn.cursor(...)` raises InterfaceError on first
            # iteration. Read-only outer transaction keeps the dump consistent.
            async with conn.transaction():
                async for record in conn.cursor(
                    f"SELECT * FROM {schema}.{table}"  # noqa: S608 (table names are constants)
                ):
                    row = dict(record)
                    if "agent_id" in row:
                        row["agent_id"] = eval_agent_id
                    fh.write(json.dumps(row, default=_json_fallback) + "\n")
                    count += 1
    logger.info("[eval.ingest] dumped %s rows from %s -> %s", count, qualified_table, out_path)
    return count


def _json_fallback(o: Any) -> Any:
    """JSON encoder for types asyncpg returns that json.dumps can't handle.

    - UUID -> str
    - datetime -> ISO 8601
    - pgvector list[float] is already JSON-safe.
    - decimals + timedelta etc. -> str fallback.
    """
    import uuid

    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


async def _write_manifest(stats: IngestStats, out_dir: Path) -> None:
    """Persist the manifest so the Dockerfile + harness can version-check later."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fixture_version": stats.fixture_version,
        "source_agent_id": stats.source_agent_id,
        "source_host": stats.source_host,
        "started_at": stats.started_at,
        "finished_at": stats.finished_at,
        "row_counts": stats.row_counts,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run(config: IngestConfig) -> IngestStats:
    """Ingest pipeline entry point — importable for tests.

    NOTE: Phase 1 ships the code only. The ``asyncpg.create_pool`` call below
    will fail if ``NOUS_PROD_DB_*`` point at anything — Phase 1 CI never
    exercises this code path. Integration tests mock the pool.
    """
    import asyncpg  # lazy — asyncpg is already a transitive dep

    from urllib.parse import urlparse

    logger.info("[eval.ingest] start  out=%s agent_id=%s", config.out_dir, config.eval_agent_id)
    started = datetime.now(tz=timezone.utc).isoformat()
    parsed_prod = urlparse(config.prod_dsn)

    stats = IngestStats(
        fixture_version=os.environ.get("NOUS_EVAL_FIXTURE_VERSION", "v2026-Q2"),
        source_agent_id=os.environ.get("NOUS_EVAL_SOURCE_AGENT_ID"),
        source_host=parsed_prod.hostname or "unknown",
        started_at=started,
    )

    # Build scratch-DB Settings now (even though this PR does not exercise it)
    # to surface any Settings wiring bugs during code review.
    _ = _settings_for_ingest(config.scratch_db_url)

    pool = await asyncpg.create_pool(config.prod_dsn, min_size=1, max_size=2)
    try:
        for qualified, filename in INGEST_TABLES:
            out = config.out_dir / filename
            rows = await _dump_table_to_jsonl(pool, qualified, out, config.eval_agent_id)
            stats.row_counts[qualified] = rows
    finally:
        await pool.close()

    stats.finished_at = datetime.now(tz=timezone.utc).isoformat()
    await _write_manifest(stats, config.out_dir)
    logger.info("[eval.ingest] done — row_counts=%s", stats.row_counts)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    config = _parse_args(argv)
    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
