#!/usr/bin/env python
"""F086: backfill ICL exemplar facts from stored episode_chunks. Zero-LLM
(parse-only), mirrors scripts/backfill_enumerative_facts.py's dry-run
conventions and scripts/backfill_r3_entity_keys.py's DB-clock watermark +
rollback phase conventions.

Reads heart.episode_chunks (NOT episodes.transcript -- transcript capture
is capped at NOUS_TRANSCRIPT_MAX_CHARS=8000 chars at ingest, but the
~400k-char exemplar streams these episodes carry persist losslessly into
chunks; see docs/features/F086-icl-exemplar-mode.md). Each chunk is parsed
INDEPENDENTLY (chunk-boundary fragments are absorbed downstream by Leg-2
native-cosine dedup + the label-aware different-label guard -- fragments
carry their true label and are harmless to ranking per the MAB
measurement), but an episode's QUALIFICATION (is_exemplar_stream) runs on
the CONCATENATED chunk text -- a single chunk can look diluted at a
boundary split even when the whole episode is a genuine exemplar stream.
`source_ordinal` continues across chunk boundaries (a running per-episode
offset), NOT reset per chunk -- this backfill assembles its own
ordinal-continuous pair list (build_episode_pairs) rather than calling
nous/handlers/exemplar_ingest.py's ingest_exemplars once per chunk, since
ingest_exemplars parses one transcript in a single call and always starts
ordinals at 0. The actual cap+embed+learn storage step IS shared with
ingest_exemplars via the common `_embed_and_store_pairs` helper -- only
the pair-assembly step differs between the two callers.

Idempotent: re-running re-embeds identical content and Heart.learn's
native-cosine dedup (0.95 threshold) confirms rather than duplicates,
except the label-aware guard (facts.py) never drops a different-label
near-duplicate -- exactly the write-path's own re-run behavior.

SMOKE TEST FIRST: --dry-run, then --max-episodes 2, before a full live run
(threshold-yield discipline -- classification thresholds can behave
differently at scale than on a 2-episode sample).

ROLLBACK: all facts written by this script (and by the live write path,
NOUS_EXEMPLAR_EXTRACTION_ENABLED) carry source='exemplar_extractor'.
    --phase rollback --watermark <iso-ts>  (the "ROLLBACK KEY" printed at
    the start of the run you want to undo) soft-deactivates:
        UPDATE heart.facts SET active = false
        WHERE agent_id = :a AND source = 'exemplar_extractor'
          AND created_at >= :watermark;
    (never hard-delete; reactivation is the inverse.) Aborts (no writes)
    if any of those facts derive from episode_chunks THEMSELVES newer than
    the watermark (i.e. content that did not exist yet when this run
    started) unless --include-live-writes is passed -- such a fact was
    plausibly produced by a concurrent live write (NOUS_EXEMPLAR_
    EXTRACTION_ENABLED processing a freshly-completed episode), not by the
    run being rolled back.

The printed watermark is ALWAYS sourced from the DATABASE's own clock
(`fetch_db_now`, `SELECT now()`), never the app host's `datetime.now()`.
The watermark is later compared against a DB-generated column
(`heart.facts.created_at` / `heart.episode_chunks.created_at`) by
`--phase rollback`; if the app host's clock is even slightly AHEAD of the
database server's, rows THIS run inserts get a DB-assigned `created_at`
EARLIER than a client-sourced watermark, so a later `--phase rollback
--watermark <that-value>`'s `created_at >= :watermark` predicate would
silently miss rows from the very run it should undo (the same clock-skew
lesson `scripts/backfill_r3_entity_keys.py`'s `fetch_db_now` fixed for R3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import text

from nous.config import Settings

# _embed_and_store_pairs is the shared cap+embed+learn implementation this
# backfill's _store_episode_pairs wraps -- see that function's docstring
# for why the two callers can't just call ingest_exemplars directly
# (ordinal continuation across chunk boundaries).
from nous.handlers.exemplar_ingest import _embed_and_store_pairs
from nous.heart.exemplars import ExemplarPair, is_exemplar_stream, parse_exemplars
from nous.storage.database import Database

logger = logging.getLogger("exemplar-backfill")


class ChunkRow(NamedTuple):
    """One heart.episode_chunks row -- matches the SQL SELECT's column
    names, so a real SQLAlchemy Row and this NamedTuple are interchangeable
    inputs to group_chunks_by_episode."""

    episode_id: UUID
    chunk_index: int
    content: str


def group_chunks_by_episode(rows) -> dict[UUID, list[str]]:
    """Group chunk rows by episode_id, contents ordered by chunk_index.

    Accepts anything with .episode_id/.chunk_index/.content attributes (a
    ChunkRow or a real SQLAlchemy Row) in any input order -- each episode's
    own chunks are explicitly sorted by chunk_index here.
    """
    by_episode: dict[UUID, list] = {}
    for row in rows:
        by_episode.setdefault(row.episode_id, []).append(row)
    return {
        episode_id: [r.content for r in sorted(chunk_rows, key=lambda r: r.chunk_index)]
        for episode_id, chunk_rows in by_episode.items()
    }


def episode_qualifies(chunk_contents: list[str], threshold: float) -> bool:
    """True when the episode's CONCATENATED chunk text passes
    is_exemplar_stream. Qualification runs at episode granularity, not
    per-chunk -- a lone chunk can look diluted (e.g. a boundary-split
    partial pair) even when the full episode is a genuine exemplar
    stream."""
    return is_exemplar_stream("\n".join(chunk_contents), threshold)


def build_episode_pairs(chunk_contents: list[str]) -> list[ExemplarPair]:
    """Parse each chunk INDEPENDENTLY, then re-stamp ordinals with a
    running per-episode offset so `source_ordinal` is one continuous
    sequence across the whole episode, never reset at a chunk boundary.
    Chunk-boundary fragments (a pair split mid-utterance) simply drop the
    dangling half with no label -- parse_exemplars already skips
    label-less utterances -- and are harmless to downstream ranking (the
    MAB measurement's own finding)."""
    pairs: list[ExemplarPair] = []
    offset = 0
    for content in chunk_contents:
        chunk_pairs = parse_exemplars(content)
        pairs.extend(replace(p, ordinal=p.ordinal + offset) for p in chunk_pairs)
        offset += len(chunk_pairs)
    return pairs


async def fetch_db_now(session) -> datetime:
    """Fetch the current time from the DATABASE's own clock (`SELECT
    now()`), never the app host's. Every timestamp this script compares
    against the rollback watermark (`heart.facts.created_at`,
    `heart.episode_chunks.created_at`) is a DB-generated column --
    sourcing the watermark itself from `datetime.now()` on the app host
    risks clock skew silently breaking the `created_at >= watermark`
    rollback predicate (mirrors `scripts/backfill_r3_entity_keys.py`'s
    identically-named helper). Returns a timezone-aware datetime
    (Postgres `timestamptz` maps to one automatically).
    """
    return (await session.execute(text("SELECT now()"))).scalar_one()


async def select_backfill_chunks(
    session, agent_id: str, since, limit: int, source_kinds: list[str] | tuple[str, ...] = ("dialogue",)
) -> list:
    """Return (episode_id, chunk_index, content) rows for up to `limit`
    DISTINCT episodes (oldest-first by started_at; 0 = all), scoped to
    agent_id. Mirrors the enumerative backfill's episode-eligibility
    predicate (select_backfill_episodes: open OR closed, never abandoned)
    then joins to episode_chunks for content instead of transcript.

    Codex r5: only chunks whose ``source_kind`` is in ``source_kinds`` are read
    (default ``('dialogue',)``). A DOCUMENT/CODE chunk (F024 attachment,
    ``ingest_document``) that happens to carry ``label:`` lines must not qualify
    an episode and pollute the ICL exemplar corpus. Old rows default 'dialogue'
    (migration 052), matching the transcript-backfill contract; widen only
    deliberately via ``--source-kinds`` when backfilling an attachment corpus.
    """
    params: dict = {"agent_id": agent_id, "source_kinds": list(source_kinds)}
    since_clause = ""
    if since is not None:
        since_clause = "AND started_at >= :since"
        params["since"] = since
    limit_clause = ""
    if limit > 0:
        limit_clause = "LIMIT :limit"
        params["limit"] = limit

    result = await session.execute(
        text(
            f"""
            WITH eligible_episodes AS (
                SELECT id
                FROM heart.episodes
                WHERE agent_id = :agent_id
                  AND (active = true OR ended_at IS NOT NULL)
                  AND outcome IS DISTINCT FROM 'abandoned'
                  {since_clause}
                ORDER BY started_at ASC
                {limit_clause}
            )
            SELECT ec.episode_id, ec.chunk_index, ec.content
            FROM heart.episode_chunks ec
            JOIN eligible_episodes e ON e.id = ec.episode_id
            WHERE ec.agent_id = :agent_id
              AND ec.source_kind = ANY(CAST(:source_kinds AS text[]))
            ORDER BY ec.episode_id, ec.chunk_index
            """
        ),
        params,
    )
    return result.all()


def _manifest_path_for(agent_id: str, watermark: str) -> str:
    """Codex r8: deterministic rollback-manifest path for a backfill run,
    derived from agent_id + the DB-clock watermark so it can be printed
    up-front (alongside the ROLLBACK KEY) before any write. Lives under the
    ``reports/`` dir next to the eval reports."""
    safe = re.sub(r"[^0-9A-Za-z]+", "_", f"{agent_id}_{watermark}")
    return str(Path("reports") / f"f086_exemplar_backfill_{safe}.jsonl")


def _read_manifest_ids(path: str) -> list[UUID]:
    """Codex r8: read the fact ids from a backfill rollback manifest (JSONL,
    one ``{"fact_id": "<uuid>"}`` object per line)."""
    ids: list[UUID] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ids.append(UUID(json.loads(line)["fact_id"]))
    return ids


async def _store_episode_pairs(
    heart, settings, pairs: list[ExemplarPair], episode_id: UUID, logger
) -> tuple[int, int, list[UUID]]:
    """Cap + embed + learn one episode's full (already ordinal-continuous)
    pair list. Thin wrapper around ``_embed_and_store_pairs``
    (nous/handlers/exemplar_ingest.py) -- the same cap+embed+learn
    implementation ``ingest_exemplars`` uses, shared rather than
    duplicated. Kept as its own function (rather than calling the shared
    helper directly from ``_run_backfill``'s loop) for this backfill's own
    log-message prefix and callsite clarity. Operates over pairs already
    assembled from MULTIPLE chunks with continuing ordinals
    (``build_episode_pairs``) -- calling ``ingest_exemplars`` once per
    chunk would instead reset each chunk's ordinals to 0, breaking
    cross-chunk continuation.
    """
    return await _embed_and_store_pairs(
        heart,
        settings,
        pairs,
        episode_id,
        logger,
        log_prefix="F086 exemplar backfill",
    )


async def rollback_exemplar_facts(
    session,
    *,
    agent_id: str,
    watermark: datetime,
    dry_run: bool,
    include_live_writes: bool = False,
) -> dict[str, int]:
    """Soft-deactivate exemplar facts created at/after `watermark` (the
    "ROLLBACK KEY" printed by the run being undone). See module docstring
    for the live-write guard's rationale. Never commits -- the caller does.
    """
    n_live_write_facts = (
        await session.execute(
            text(
                "SELECT COUNT(DISTINCT f.id) FROM heart.facts f "
                "JOIN heart.episode_chunks ec ON ec.episode_id = f.source_episode_id "
                "WHERE f.agent_id = :a AND f.source = 'exemplar_extractor' "
                "AND f.created_at >= :w AND ec.created_at >= :w"
            ),
            {"a": agent_id, "w": watermark},
        )
    ).scalar_one()

    if dry_run:
        n_facts = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM heart.facts "
                    "WHERE agent_id = :a AND source = 'exemplar_extractor' AND created_at >= :w"
                ),
                {"a": agent_id, "w": watermark},
            )
        ).scalar_one()
        return {"facts_deactivated": n_facts, "live_write_facts": n_live_write_facts}

    if n_live_write_facts > 0 and not include_live_writes:
        raise RuntimeError(
            f"Rollback aborted: {n_live_write_facts} exemplar fact(s) derive from "
            "episode_chunks that are THEMSELVES newer than the watermark -- content "
            "that did not exist when this run started, so these facts were plausibly "
            "produced by a concurrent live write (NOUS_EXEMPLAR_EXTRACTION_ENABLED), "
            "not by the backfill run being rolled back. Re-run with "
            "--include-live-writes to proceed anyway."
        )

    result = await session.execute(
        text(
            "UPDATE heart.facts SET active = false "
            "WHERE agent_id = :a AND source = 'exemplar_extractor' AND created_at >= :w"
        ),
        {"a": agent_id, "w": watermark},
    )
    return {"facts_deactivated": result.rowcount, "live_write_facts": n_live_write_facts}


async def rollback_exemplar_facts_by_manifest(
    session,
    *,
    agent_id: str,
    fact_ids: list[UUID],
    dry_run: bool,
) -> dict[str, int]:
    """Codex r8: soft-deactivate EXACTLY the exemplar facts named in a backfill
    manifest (agent- and source-scoped, active rows only). This is the exact
    rollback mode -- unlike the watermark fallback it cannot catch a concurrent
    live write, because a live write's fact id is never in a backfill manifest.
    Never commits -- the caller does.
    """
    if not fact_ids:
        return {"facts_deactivated": 0}
    ids = [str(i) for i in fact_ids]
    if dry_run:
        n = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM heart.facts WHERE agent_id = :a "
                    "AND source = 'exemplar_extractor' AND active = true "
                    "AND id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"a": agent_id, "ids": ids},
            )
        ).scalar_one()
        return {"facts_deactivated": n}
    result = await session.execute(
        text(
            "UPDATE heart.facts SET active = false WHERE agent_id = :a "
            "AND source = 'exemplar_extractor' AND active = true "
            "AND id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"a": agent_id, "ids": ids},
    )
    return {"facts_deactivated": result.rowcount}


async def _run_backfill(
    *,
    agent_id: str,
    since,
    max_episodes: int,
    density_threshold: float | None,
    dry_run: bool,
    source_kinds: list[str] | tuple[str, ...] = ("dialogue",),
) -> int:
    settings = Settings()

    # Live mode requires an embedding key -- without it facts are stored
    # with NULL embeddings and Leg-2 cosine dedup never runs, so re-runs
    # duplicate the entire fact set (idempotency depends on embedding dedup).
    if not dry_run and not settings.openai_api_key:
        print(
            "ERROR: live backfill requires OPENAI_API_KEY -- idempotency depends on embedding dedup.",
            file=sys.stderr,
        )
        return 2

    threshold = density_threshold if density_threshold is not None else settings.exemplar_density_threshold

    db = Database(settings)
    await db.connect()
    try:
        # Watermark fetched from the DB's own clock (see fetch_db_now's
        # docstring), immediately after connect and before any write --
        # as close as possible to the first write this run could make.
        async with db.session() as session:
            watermark = (await fetch_db_now(session)).isoformat()
        print(f"ROLLBACK KEY (created_at watermark): {watermark}")
        # Codex r8: the EXACT rollback manifest path (preferred over the
        # watermark, which cannot distinguish a concurrent live write). Computed
        # up-front so it is printed even if the run later fails.
        manifest_path = _manifest_path_for(agent_id, watermark)
        print(f"ROLLBACK MANIFEST (exact fact ids, preferred): {manifest_path}")

        async with db.session() as session:
            rows = await select_backfill_chunks(session, agent_id, since, max_episodes, source_kinds)

        by_episode = group_chunks_by_episode(rows)
        qualifying = {
            episode_id: contents
            for episode_id, contents in by_episode.items()
            if episode_qualifies(contents, threshold)
        }

        if dry_run:
            total_pairs = sum(len(build_episode_pairs(contents)) for contents in qualifying.values())
            print(
                f"DRY RUN [source_kinds={','.join(source_kinds)}]: {len(by_episode)} episodes with chunks, "
                f"{len(qualifying)} classified exemplar streams, "
                f"{total_pairs} candidate pairs -- no writes. "
                f"Would write rollback manifest to {manifest_path}."
            )
            return 0

        from nous.brain.embeddings import EmbeddingProvider
        from nous.heart.heart import Heart

        embedder = EmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            cache_size=settings.embedding_cache_size,
        )
        heart = Heart(database=db, settings=settings, embedding_provider=embedder, owns_embeddings=False)
        # Override agent_id from CLI arg (Settings() reads it from env).
        heart.facts.agent_id = agent_id

        total = 0
        total_skipped = 0
        created_ids: list[UUID] = []
        async with heart:
            for episode_id, contents in qualifying.items():
                pairs = build_episode_pairs(contents)
                stored, skipped, ids = await _store_episode_pairs(heart, settings, pairs, episode_id, logger)
                total += stored
                total_skipped += skipped
                created_ids.extend(ids)

        # Codex r8: write the EXACT rollback manifest (one fact id per JSONL
        # line). --phase rollback --manifest deactivates exactly these ids, so a
        # concurrent live write (never in this manifest) is never touched.
        Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as fh:
            for fid in created_ids:
                fh.write(json.dumps({"fact_id": str(fid)}) + "\n")

        print(
            f"Backfilled {total} exemplar facts across {len(qualifying)} episodes "
            f"({total_skipped} skipped: no embedding). "
            f"Wrote {len(created_ids)} fact id(s) to rollback manifest {manifest_path}."
        )
        return 0
    except Exception:
        logger.exception("Backfill failed")
        return 2
    finally:
        await db.disconnect()


async def _run_rollback(
    *,
    agent_id: str,
    watermark: datetime | None,
    manifest_path: str | None,
    dry_run: bool,
    include_live_writes: bool,
) -> int:
    settings = Settings()
    db = Database(settings)
    await db.connect()
    try:
        async with db.session() as session:
            if manifest_path is not None:
                fact_ids = _read_manifest_ids(manifest_path)
                counts = await rollback_exemplar_facts_by_manifest(
                    session, agent_id=agent_id, fact_ids=fact_ids, dry_run=dry_run
                )
                mode = "manifest"
            else:
                # Codex r8: watermark is the FALLBACK -- it keys on created_at and
                # cannot distinguish a concurrent live write from a backfill row.
                print(
                    "WARNING: --watermark rollback is the FALLBACK mode. It deactivates ALL "
                    "exemplar facts created at/after the watermark by created_at, which MAY catch "
                    "concurrent live writes (the chunk-timestamp guard only catches post-watermark "
                    "chunks). Prefer --manifest for an EXACT rollback.",
                    file=sys.stderr,
                )
                counts = await rollback_exemplar_facts(
                    session,
                    agent_id=agent_id,
                    watermark=watermark,
                    dry_run=dry_run,
                    include_live_writes=include_live_writes,
                )
                mode = "watermark"
            if not dry_run:
                await session.commit()
        label = "DRY RUN " if dry_run else ""
        extra = f" live_write_facts={counts['live_write_facts']}" if "live_write_facts" in counts else ""
        print(f"[rollback:{mode}] {label}facts_deactivated={counts['facts_deactivated']}{extra}")
        return 0
    except Exception:
        logger.exception("Rollback failed")
        return 2
    finally:
        await db.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="F086: backfill ICL exemplar facts from stored episode_chunks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ROLLBACK: --phase rollback --watermark <iso-ts> (the 'ROLLBACK KEY'\n"
            "printed by the run you want to undo). See the module docstring for the\n"
            "live-write guard. SMOKE TEST FIRST: --dry-run, then --max-episodes 2,\n"
            "before a full live run."
        ),
    )
    parser.add_argument(
        "--agent-id",
        required=True,
        help="Agent identifier (e.g. nous-default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count episodes/pairs; no writes.",
    )
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=0,
        help="Max episodes to process (0 = all).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO date (YYYY-MM-DD); only episodes started on or after this date.",
    )
    parser.add_argument(
        "--density-threshold",
        type=float,
        default=None,
        help="Override exemplar density threshold (0.0-1.0).",
    )
    parser.add_argument(
        "--source-kinds",
        default="dialogue",
        help="Comma-separated episode_chunks source_kind(s) to read (default 'dialogue'). "
        "Valid: dialogue, document, code (migration-052 CHECK set). Widen to 'dialogue,document' "
        "ONLY to deliberately backfill an attachment/document corpus -- document/code chunks with "
        "label: lines would otherwise pollute the dialogue ICL corpus.",
    )
    parser.add_argument(
        "--phase",
        choices=["backfill", "rollback"],
        default="backfill",
        help="'backfill' (default) or 'rollback' (requires --watermark).",
    )
    parser.add_argument(
        "--manifest",
        type=str,
        default=None,
        help="Path to the exact rollback manifest (the 'ROLLBACK MANIFEST' JSONL "
        "printed by a prior backfill run). PREFERRED for --phase rollback -- "
        "deactivates exactly the fact ids it created, so a concurrent live write "
        "is never touched. Ignored outside --phase rollback.",
    )
    parser.add_argument(
        "--watermark",
        type=str,
        default=None,
        help="ISO-8601, timezone-aware timestamp (the 'ROLLBACK KEY' printed by "
        "a prior run). FALLBACK for --phase rollback when no manifest is available "
        "(warns -- may catch concurrent live writes). Ignored otherwise.",
    )
    parser.add_argument(
        "--include-live-writes",
        action="store_true",
        help="--phase rollback normally ABORTS (no writes) when any exemplar fact "
        "derives from episode_chunks newer than the watermark -- plausibly a "
        "concurrent live write. Pass this to proceed anyway. Ignored outside "
        "--phase rollback.",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.phase == "rollback":
        # Codex r8: manifest is the preferred exact mode; watermark is the
        # fallback. Require one. If both are given, manifest wins.
        if not args.manifest and not args.watermark:
            parser.error("--phase rollback requires --manifest <path> (preferred) or --watermark <iso-ts> (fallback)")
        watermark = None
        manifest_path = None
        if args.manifest:
            if not os.path.exists(args.manifest):
                parser.error(f"--manifest file not found: {args.manifest!r}")
            manifest_path = args.manifest
        else:
            try:
                watermark = datetime.fromisoformat(args.watermark)
            except ValueError:
                parser.error(f"--watermark is not a valid ISO-8601 timestamp: {args.watermark!r}")
            if watermark.tzinfo is None:
                parser.error("--watermark must be timezone-aware (e.g. include '+00:00' or 'Z')")
        sys.exit(
            asyncio.run(
                _run_rollback(
                    agent_id=args.agent_id,
                    watermark=watermark,
                    manifest_path=manifest_path,
                    dry_run=args.dry_run,
                    include_live_writes=args.include_live_writes,
                )
            )
        )

    since = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(
                f"ERROR: --since {args.since!r} is not a valid ISO date (YYYY-MM-DD).",
                file=sys.stderr,
            )
            sys.exit(2)

    # Codex r5: validate --source-kinds against the migration-052 CHECK set so a
    # typo fails loudly here rather than silently reading zero chunks.
    _VALID_SOURCE_KINDS = ("dialogue", "document", "code")
    source_kinds = [k.strip() for k in args.source_kinds.split(",") if k.strip()]
    invalid = [k for k in source_kinds if k not in _VALID_SOURCE_KINDS]
    if not source_kinds or invalid:
        parser.error(
            f"--source-kinds must be a comma-separated subset of {','.join(_VALID_SOURCE_KINDS)} "
            f"(got {args.source_kinds!r})"
        )

    sys.exit(
        asyncio.run(
            _run_backfill(
                agent_id=args.agent_id,
                since=since,
                max_episodes=args.max_episodes,
                density_threshold=args.density_threshold,
                dry_run=args.dry_run,
                source_kinds=source_kinds,
            )
        )
    )


if __name__ == "__main__":
    main()
