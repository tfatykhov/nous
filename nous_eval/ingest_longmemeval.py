"""F051 LongMemEval_S ingest — download + stratify + replay.

Phase 1: code only. Phase 2: operator runs this module to populate
``docs/features/fixtures/longmemeval_qrels.jsonl`` and extend the corpus
with 20 stratified chat-history-replayed episodes + extracted facts.

Pipeline:

1. Download ``longmemeval_data/longmemeval_s_cleaned.json`` from the LongMemEval
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
import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 2026-04-26 update: upstream moved from GitHub raw to Hugging Face dataset
# repo. The cleaned variant ("longmemeval_s_cleaned.json") supersedes the
# original — same schema, sessions cleaned to remove answer-leakage. See
# repo README: https://github.com/xiaowu0162/LongMemEval (data section).
LONGMEMEVAL_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
    "resolve/main/longmemeval_s_cleaned.json"
)
# SHA-256 of the upstream file. Empty = skip verification (first download).
# Updated when upstream changes; mismatch aborts the download (fail-closed).
LONGMEMEVAL_SHA256 = ""

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


def _default_out_qrels() -> Path:
    """F051.5: source_registry expects ``<NOUS_EVAL_FIXTURES_DIR>/qrels_longmemeval.jsonl``.

    Falls back to ``tests/fixtures`` when the env var is unset (Phase-1 default
    location, kept for back-compat with smoke tests).
    """
    fixtures_dir = os.environ.get("NOUS_EVAL_FIXTURES_DIR")
    base = Path(fixtures_dir) if fixtures_dir else Path("tests/fixtures")
    return base / "qrels_longmemeval.jsonl"


@dataclass
class IngestLMEConfig:
    """Operator inputs for LongMemEval ingest."""

    n: int = DEFAULT_N
    cache_dir: Path = DEFAULT_CACHE_DIR
    out_qrels: Path = field(default_factory=_default_out_qrels)
    scratch_db_url: str = "postgresql+asyncpg://nous:nous_eval@localhost:5433/nous_eval_scratch"
    skip_download: bool = False
    seed: int = 0
    max_sessions_per_question: int = 10  # F051.5: cost guardrail (devil P2)
    # F051.5.x: paced batching. Default 0 = no sleep (matches original behavior).
    # When >0, replay sleeps for this many seconds after every
    # `inter_question_batch` questions complete, spreading Sonnet load and
    # giving operators a clean break-point if they want to Ctrl-C between batches.
    inter_question_sleep_seconds: float = 0.0
    inter_question_batch: int = 5


@dataclass
class LMEIngestStats:
    picked_question_ids: list[str] = field(default_factory=list)
    per_type_counts: dict[str, int] = field(default_factory=dict)
    n_sessions_replayed: int = 0
    n_sessions_reused: int = 0  # F051.5: cross-question episode dedup hits
    n_facts_extracted: int = 0
    n_episodes_summarised: int = 0


def _session_to_transcript(session: list | dict) -> str:
    """F051.5: render LongMemEval session as a transcript matching prod handler format.

    LongMemEval sessions are either a list of turns or a dict with a "turns" key.
    Prod handlers expect ``"\\n\\n"``-joined ``"role: content"`` lines with at least
    50 chars total (see episode_summarizer.py:118).
    """
    turns = session if isinstance(session, list) else (session.get("turns") or [])
    return "\n\n".join(
        f"{t.get('role', 'user')}: {t.get('content', '')}"
        for t in turns
        if t.get("content")
    )


# ---------------------------------------------------------------------------
# Download + cache
# ---------------------------------------------------------------------------


def _download_if_missing(cache_dir: Path, url: str, expected_sha: str | None) -> Path:
    """Fetch ``url`` to ``cache_dir/longmemeval_s_cleaned.json`` once; return path.

    Uses ``httpx`` if available (already a Nous dep), falls back to ``urllib``.
    When ``expected_sha`` is non-empty, abort on hash mismatch — prevents a
    silent upstream change from polluting the qrels set.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "longmemeval_s_cleaned.json"
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
    max_sessions_per_question: int = 10,
    inter_question_sleep_seconds: float = 0.0,
    inter_question_batch: int = 5,
) -> tuple[LMEIngestStats, dict[str, dict[int, dict[str, list]]]]:
    """F051.5 Phase 2: actually replay each haystack session into the scratch DB.

    For each picked question, walks haystack_sessions; for each session creates
    an Episode via ``Heart.start_episode``, calls ``EpisodeSummarizer.summarize_episode``
    directly (no bus), then ``FactExtractor.extract_and_store`` directly. Records
    the resulting episode + fact UUIDs into a provenance map that ``_write_qrels``
    consumes to populate ``gold_ids``.

    Cross-question episode dedup: hashes session content; if the same conversation
    appears in another question's haystack (LongMemEval reuses sessions across
    paraphrase questions), reuse the cached UUIDs rather than ingesting twice.

    Returns:
        ``(stats, provenance)`` where
        ``provenance[question_id][session_idx] = {"episode": [UUID], "fact": [UUID]}``.
    """
    from nous.brain.embeddings import EmbeddingProvider
    from nous.heart.heart import Heart
    from nous.heart.schemas import EpisodeInput
    from nous.handlers.episode_summarizer import EpisodeSummarizer
    from nous.handlers.fact_extractor import FactExtractor
    from nous.storage.database import Database
    from nous_eval.ingest import _settings_for_ingest

    settings = _settings_for_ingest(scratch_db_url)
    if not settings.openai_api_key:
        raise SystemExit("F051.5: OPENAI_API_KEY required for replay")
    # Dedicated agent_id partitions LongMemEval episodes from the F051
    # stratified corpus (both live in the scratch DB; same agent_id would
    # cross-pollute Heart.recall results).
    settings = settings.model_copy(update={"agent_id": "nous-lme-corpus"})

    db = Database(settings)
    stats = LMEIngestStats()
    provenance: dict[str, dict[int, dict[str, list]]] = {}
    try:
        await db.connect()
        embedder = EmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        heart = Heart(database=db, settings=settings, embedding_provider=embedder)
        # bus=None: handlers' constructors tolerate this (F051.5 refactor).
        summarizer = EpisodeSummarizer(heart=heart, brain=None, settings=settings, bus=None)
        extractor = FactExtractor(heart=heart, settings=settings, bus=None)

        # Cross-question episode dedup: keyed on session-content hash.
        seen_session_episode: dict[str, Any] = {}
        seen_session_facts: dict[str, list] = {}

        for q_idx, q in enumerate(picked):
            qid = q.get("question_id", "")
            provenance[qid] = {}
            sessions = (q.get("haystack_sessions") or [])[:max_sessions_per_question]
            # Cleaned upstream (2026-04-26 onward): haystack_session_ids is a
            # parallel string-ID array; answer_session_ids references THESE
            # strings, NOT integer indices. Pre-cleaned upstream had no such
            # field — fall back to integer-index keying for back-compat.
            session_ids = q.get("haystack_session_ids") or []
            for session_idx, session in enumerate(sessions):
                # Per-session key for provenance lookup. Prefer the string ID
                # when present (cleaned upstream); fall back to int index.
                session_key = (
                    session_ids[session_idx]
                    if session_idx < len(session_ids)
                    else session_idx
                )
                transcript = _session_to_transcript(session)
                if not transcript:
                    logger.debug("F051.5: qid=%s session=%d empty transcript, skipping", qid, session_idx)
                    continue
                session_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()

                # Reuse cached UUIDs when the same session appeared in a prior question.
                if session_hash in seen_session_episode:
                    provenance[qid][session_key] = {
                        "episode": [seen_session_episode[session_hash]],
                        "fact": list(seen_session_facts[session_hash]),
                    }
                    stats.n_sessions_reused += 1
                    continue

                # 1. Materialize episode (EpisodeInput.summary required).
                ep = await heart.start_episode(EpisodeInput(
                    summary=transcript[:500] or "(empty)",
                    trigger="lme_replay",
                    tags=["longmemeval", q.get("question_type", "")],
                ))

                # 2. Summarize directly. Returns None on early-return / LLM error.
                summary = await summarizer.summarize_episode(
                    episode_id=ep.id,
                    transcript=transcript,
                    agent_id=settings.agent_id,
                )
                fact_ids: list = []
                if summary is not None:
                    # 3. Extract facts directly. Returns canonical UUIDs even on dedup.
                    fact_ids = await extractor.extract_and_store(
                        summary=summary,
                        episode_id=str(ep.id),
                        transcript=transcript,
                    )
                    stats.n_episodes_summarised += 1
                    stats.n_facts_extracted += len(fact_ids)

                # 4. End episode with VALID outcome value (EpisodeOutcome Literal
                # accepts {success, partial, failure, ongoing, abandoned} — no
                # custom values). Episode has tags=["longmemeval", ...] so any
                # future filter can use the tag, not the outcome.
                await heart.end_episode(ep.id, outcome="success")

                # 5. Cache + provenance.
                seen_session_episode[session_hash] = ep.id
                seen_session_facts[session_hash] = list(fact_ids)
                provenance[qid][session_key] = {
                    "episode": [ep.id],
                    "fact": fact_ids,
                }
                stats.n_sessions_replayed += 1
            stats.picked_question_ids.append(qid)

            # F051.5.x: paced batching. After every `inter_question_batch`
            # questions, sleep `inter_question_sleep_seconds` so OAT rate
            # limits + operator interrupt windows have natural break-points.
            # Skip the sleep on the very last question (no point waiting).
            completed = q_idx + 1
            if (
                inter_question_sleep_seconds > 0
                and inter_question_batch > 0
                and completed < len(picked)
                and completed % inter_question_batch == 0
            ):
                logger.info(
                    "F051.5: completed %d/%d questions (n_sessions_replayed=%d, "
                    "n_sessions_reused=%d), sleeping %.1fs before next batch",
                    completed, len(picked),
                    stats.n_sessions_replayed, stats.n_sessions_reused,
                    inter_question_sleep_seconds,
                )
                await asyncio.sleep(inter_question_sleep_seconds)
        return stats, provenance
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Qrels writer
# ---------------------------------------------------------------------------


def _write_qrels(
    picked: list[dict[str, Any]],
    stats: LMEIngestStats,
    provenance: dict[str, dict[int, dict[str, list]]],
    out_path: Path,
) -> None:
    """F051.5: emit one qrel per picked question with populated ``gold_ids``.

    For each question, gold_ids = (episodes ∪ facts) UUIDs from the haystack
    sessions named in the question's ``answer_session_ids`` field. Sessions
    whose answer_session_id is non-int or out-of-range are logged WARN and
    skipped; questions where the resulting gold_ids is empty are emitted
    anyway so downstream code can count "missing-gold" qrels separately.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_missing_gold = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for q in picked:
            qid = q.get("question_id", "")
            answer_sids = q.get("answer_session_ids") or []
            gold_ids: list[str] = []
            qid_provenance = provenance.get(qid, {})
            for sid in answer_sids:
                # Cleaned upstream uses string session IDs (e.g. "answer_280352e9");
                # pre-cleaned upstream used 0-based integer indices. Try both.
                ids_for_session = None
                if sid in qid_provenance:
                    ids_for_session = qid_provenance[sid]
                else:
                    try:
                        idx = int(sid)
                        if idx in qid_provenance:
                            ids_for_session = qid_provenance[idx]
                    except (TypeError, ValueError):
                        pass
                if ids_for_session is None:
                    logger.warning(
                        "F051.5: qid=%s answer_session_id %r not found in "
                        "provenance map (was the session capped by "
                        "--max-sessions-per-question?) — skipping",
                        qid, sid,
                    )
                    continue
                gold_ids.extend(str(u) for u in ids_for_session.get("episode", []))
                gold_ids.extend(str(u) for u in ids_for_session.get("fact", []))
            if not gold_ids:
                # F051.5 hotfix: skip empty-gold qrels entirely. The Qrel
                # pydantic model requires gold_ids min_length=1, so emitting
                # them would produce a JSONL file load_qrels rejects on the
                # first such row. Operator sees the count via the WARN log
                # below and the final aggregate stat.
                n_missing_gold += 1
                logger.warning(
                    "F051.5: qid=%s no gold_ids populated — answer_session_ids=%r produced 0 memories (skipping qrel emit)",
                    qid, answer_sids,
                )
                continue
            fh.write(
                json.dumps(
                    {
                        "query": q.get("question"),
                        "gold_ids": gold_ids,
                        "memory_types": ["episode", "fact"],
                        # F051.5 (devil P1): QrelSource enum value is "longmemeval",
                        # not "longmemeval_s". Phase-1 had this latent bug.
                        "source": "longmemeval",
                        "notes": {
                            "question_id": qid,
                            "question_type": q.get("question_type"),
                            "answer_session_ids": answer_sids,
                            "n_replayed_sessions": len(provenance.get(qid, {})),
                        },
                        "reviewed_by": None,
                    }
                )
                + "\n"
            )
    logger.info(
        "F051.5: wrote %d qrels to %s (%d with empty gold_ids, %d sessions reused)",
        len(picked), out_path, n_missing_gold, stats.n_sessions_reused,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> IngestLMEConfig:
    p = argparse.ArgumentParser(prog="python -m nous_eval.ingest_longmemeval")
    p.add_argument("--n", type=int, default=DEFAULT_N)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--out-qrels", type=Path, default=None)  # falls back to _default_out_qrels()
    p.add_argument("--scratch-db-url", default=os.environ.get(
        "NOUS_EVAL_SCRATCH_DB_URL",
        "postgresql+asyncpg://nous:nous_eval@localhost:5433/nous_eval_scratch",
    ))
    p.add_argument("--skip-download", action="store_true")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--max-sessions-per-question", type=int, default=10,
        help="F051.5 cost guardrail. LongMemEval haystacks can have 30-50 sessions; "
             "default 10 caps Sonnet spend at ~$15-25 for N=20.",
    )
    p.add_argument(
        "--inter-question-sleep-seconds", type=float, default=0.0,
        help="F051.5: pause this many seconds after every "
             "--inter-question-batch questions complete. Default 0 = no pacing "
             "(matches original behavior). Set to e.g. 30 to spread Sonnet load "
             "across natural break-points.",
    )
    p.add_argument(
        "--inter-question-batch", type=int, default=5,
        help="F051.5: questions per pacing batch (default 5; ~100 Sonnet calls "
             "at default --max-sessions-per-question=10).",
    )
    ns = p.parse_args(argv)
    return IngestLMEConfig(
        n=ns.n,
        cache_dir=ns.cache_dir,
        out_qrels=ns.out_qrels if ns.out_qrels is not None else _default_out_qrels(),
        scratch_db_url=ns.scratch_db_url,
        skip_download=ns.skip_download,
        seed=ns.seed,
        max_sessions_per_question=ns.max_sessions_per_question,
        inter_question_sleep_seconds=ns.inter_question_sleep_seconds,
        inter_question_batch=ns.inter_question_batch,
    )


async def run(config: IngestLMEConfig) -> LMEIngestStats:
    """Ingest entry point — importable for tests."""
    if not config.skip_download:
        src = _download_if_missing(
            config.cache_dir, LONGMEMEVAL_URL, LONGMEMEVAL_SHA256 or None
        )
    else:
        src = config.cache_dir / "longmemeval_s_cleaned.json"

    data = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"F051.5: Unexpected LongMemEval format at {src}")

    picked, counts = _stratify(data, config.n, config.seed)
    logger.info("F051.5: picked=%d types=%s", len(picked), counts)

    stats, provenance = await _replay_sessions_into_scratch(
        picked,
        config.scratch_db_url,
        max_sessions_per_question=config.max_sessions_per_question,
        inter_question_sleep_seconds=config.inter_question_sleep_seconds,
        inter_question_batch=config.inter_question_batch,
    )
    stats.per_type_counts = counts
    _write_qrels(picked, stats, provenance, config.out_qrels)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    import asyncio

    config = _parse_args(argv)
    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
