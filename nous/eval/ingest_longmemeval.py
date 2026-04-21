"""F051 LongMemEval_S ingest — download + stratify + replay.

Phase 1: code only. Phase 2: operator runs this module to populate
``docs/features/fixtures/longmemeval_qrels.jsonl`` and extend the corpus
with 20 stratified chat-history-replayed episodes + extracted facts.

Pipeline:

1. Download ``longmemeval_data/longmemeval_s.json`` from the LongMemEval
   GitHub repo (cached at ``~/.cache/nous-eval/longmemeval/``). Skip if
   already present + hash-checked.
2. Parse the file (list of ``{question_id, question_type, question,
   answer_session_ids, haystack_sessions, ...}``) and stratify by
   ``question_type`` (6 reasoning categories: single-session-user,
   single-session-assistant, single-session-preference, multi-session,
   temporal-reasoning, knowledge-update).
3. Pick N (default 20) questions, uniformly across the 6 types where possible.
4. For each picked question, replay ``haystack_sessions`` through the
   ``fact_extractor`` + ``episode_summarizer`` pipelines pointed at the
   **scratch eval DB** (never prod).
5. Emit a qrels JSONL keyed on the question, with gold_ids = the UUIDs of
   the replayed episodes/facts that contain the ``answer_session_ids``.

This file is THIN — the heavy lifting (replay) delegates to the shared
``fact_extractor`` + ``episode_summarizer`` handlers used in production, with
the EventBus disabled per ingest.py's `_settings_for_ingest` contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LONGMEMEVAL_URL = (
    "https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/"
    "longmemeval_data/longmemeval_s.json"
)
# SHA-256 of the upstream file as of 2026-04-20. Updated when upstream changes;
# mismatch aborts the download (fail-closed).
LONGMEMEVAL_SHA256 = ""  # populated in Phase 2 once the file is first downloaded

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "nous-eval" / "longmemeval"
DEFAULT_N = 20
# LongMemEval's six reasoning types. Stratified sampling picks
# ceil(N / 6) from each type then trims to exactly N.
QUESTION_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)


@dataclass
class IngestLMEConfig:
    """Operator inputs for LongMemEval ingest."""

    n: int = DEFAULT_N
    cache_dir: Path = DEFAULT_CACHE_DIR
    out_qrels: Path = Path("tests/fixtures/longmemeval_qrels.jsonl")
    scratch_db_url: str = "postgresql+asyncpg://nous:nous_eval@localhost:5433/nous_eval_scratch"
    skip_download: bool = False
    seed: int = 0


@dataclass
class LMEIngestStats:
    picked_question_ids: list[str] = field(default_factory=list)
    per_type_counts: dict[str, int] = field(default_factory=dict)
    n_sessions_replayed: int = 0
    n_facts_extracted: int = 0
    n_episodes_summarised: int = 0


# ---------------------------------------------------------------------------
# Download + cache
# ---------------------------------------------------------------------------


def _download_if_missing(cache_dir: Path, url: str, expected_sha: str | None) -> Path:
    """Fetch ``url`` to ``cache_dir/longmemeval_s.json`` once; return path.

    Uses ``httpx`` if available (already a Nous dep), falls back to ``urllib``.
    When ``expected_sha`` is non-empty, abort on hash mismatch — prevents a
    silent upstream change from polluting the qrels set.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "longmemeval_s.json"
    if target.exists():
        logger.info("[eval.ingest_lme] cached %s", target)
    else:
        logger.info("[eval.ingest_lme] downloading %s", url)
        try:
            import httpx

            with httpx.Client(timeout=60) as c:
                r = c.get(url)
                r.raise_for_status()
                target.write_bytes(r.content)
        except ImportError:  # pragma: no cover — httpx is in deps
            from urllib.request import urlopen

            with urlopen(url, timeout=60) as resp:  # noqa: S310 (allowlisted URL)
                target.write_bytes(resp.read())

    if expected_sha:
        got = hashlib.sha256(target.read_bytes()).hexdigest()
        if got != expected_sha:
            raise SystemExit(
                f"[eval.ingest_lme] LongMemEval SHA-256 mismatch:\n"
                f"  expected {expected_sha}\n"
                f"  got      {got}\n"
                "Upstream file changed. Update LONGMEMEVAL_SHA256 constant after review."
            )
    return target


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------


def _stratify(
    questions: list[dict[str, Any]],
    n: int,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Pick ``n`` questions with ~even distribution across QUESTION_TYPES.

    Deterministic under ``seed`` so re-runs produce identical qrels until the
    operator explicitly bumps the seed.

    Falls back to "pick what you can" if a type has <per_type questions.
    """
    import random

    rng = random.Random(seed)
    by_type: dict[str, list[dict[str, Any]]] = {t: [] for t in QUESTION_TYPES}
    for q in questions:
        qt = q.get("question_type")
        if qt in by_type:
            by_type[qt].append(q)

    per_type = max(1, n // len(QUESTION_TYPES))
    picked: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for qt in QUESTION_TYPES:
        bucket = by_type[qt]
        rng.shuffle(bucket)
        take = min(per_type, len(bucket))
        picked.extend(bucket[:take])
        counts[qt] = take

    if len(picked) < n:
        # Top up from any remaining questions in any type
        remaining = [
            q
            for qt in QUESTION_TYPES
            for q in by_type[qt][counts[qt] :]
        ]
        rng.shuffle(remaining)
        picked.extend(remaining[: n - len(picked)])

    picked = picked[:n]
    # Recount after top-up so the stats reflect reality.
    counts = {t: 0 for t in QUESTION_TYPES}
    for q in picked:
        counts[q.get("question_type", "unknown")] = counts.get(q.get("question_type", "unknown"), 0) + 1
    return picked, counts


# ---------------------------------------------------------------------------
# Replay (Phase 2 — stub in Phase 1)
# ---------------------------------------------------------------------------


async def _replay_sessions_into_scratch(
    picked: list[dict[str, Any]],
    scratch_db_url: str,
) -> LMEIngestStats:
    """Drive ``fact_extractor`` + ``episode_summarizer`` over each haystack session.

    Phase 1 ships the skeleton: we construct Settings for the scratch DB (via
    :func:`nous.eval.ingest._settings_for_ingest`) and log the intended actions.
    Phase 2 wires the actual replay call — blocked on a small refactor of the
    fact_extractor handler so it can run without a live EventBus.
    """
    from nous.eval.ingest import _settings_for_ingest

    # Build Settings now so mis-wiring surfaces at code-review time, not Phase 2.
    _ = _settings_for_ingest(scratch_db_url)

    stats = LMEIngestStats()
    for q in picked:
        sessions = q.get("haystack_sessions") or []
        stats.n_sessions_replayed += len(sessions)
        stats.picked_question_ids.append(q.get("question_id", ""))
        # TODO(phase-2): for each session, call:
        #     await episode_summarizer.summarise(session_text, settings, db=scratch_db)
        #     facts = await fact_extractor.extract(session_text, settings, db=scratch_db)
        # and collect the produced UUIDs for the qrels output.
        logger.debug("[eval.ingest_lme] (phase-1 stub) replay qid=%s sessions=%d",
                     q.get("question_id"), len(sessions))
    return stats


# ---------------------------------------------------------------------------
# Qrels writer
# ---------------------------------------------------------------------------


def _write_qrels(picked: list[dict[str, Any]], stats: LMEIngestStats, out_path: Path) -> None:
    """Emit one qrel row per picked question to ``out_path``.

    gold_ids is left empty in Phase 1 — Phase 2 fills it from the replayed
    UUIDs produced by ``_replay_sessions_into_scratch``.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for q in picked:
            fh.write(
                json.dumps(
                    {
                        "query": q.get("question"),
                        "gold_ids": [],  # populated in Phase 2
                        "memory_types": ["episode", "fact"],
                        "source": "longmemeval_s",
                        "notes": {
                            "question_id": q.get("question_id"),
                            "question_type": q.get("question_type"),
                            "answer_session_ids": q.get("answer_session_ids", []),
                        },
                        "reviewed_by": None,
                    }
                )
                + "\n"
            )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> IngestLMEConfig:
    p = argparse.ArgumentParser(prog="python -m nous.eval.ingest_longmemeval")
    p.add_argument("--n", type=int, default=DEFAULT_N)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--out-qrels", type=Path, default=Path("tests/fixtures/longmemeval_qrels.jsonl"))
    p.add_argument("--scratch-db-url", default=os.environ.get(
        "NOUS_EVAL_SCRATCH_DB_URL",
        "postgresql+asyncpg://nous:nous_eval@localhost:5433/nous_eval_scratch",
    ))
    p.add_argument("--skip-download", action="store_true")
    p.add_argument("--seed", type=int, default=0)
    ns = p.parse_args(argv)
    return IngestLMEConfig(
        n=ns.n,
        cache_dir=ns.cache_dir,
        out_qrels=ns.out_qrels,
        scratch_db_url=ns.scratch_db_url,
        skip_download=ns.skip_download,
        seed=ns.seed,
    )


async def run(config: IngestLMEConfig) -> LMEIngestStats:
    """Ingest entry point — importable for tests."""
    if not config.skip_download:
        src = _download_if_missing(
            config.cache_dir, LONGMEMEVAL_URL, LONGMEMEVAL_SHA256 or None
        )
    else:
        src = config.cache_dir / "longmemeval_s.json"

    data = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"[eval.ingest_lme] Unexpected LongMemEval format at {src}")

    picked, counts = _stratify(data, config.n, config.seed)
    logger.info("[eval.ingest_lme] picked=%d types=%s", len(picked), counts)

    stats = await _replay_sessions_into_scratch(picked, config.scratch_db_url)
    stats.per_type_counts = counts
    _write_qrels(picked, stats, config.out_qrels)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    import asyncio

    config = _parse_args(argv)
    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
