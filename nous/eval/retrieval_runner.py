"""Matrix runner — iterates ``configs × qrels``, calls the recall pipeline,
scores each result against gold IDs.

This module is the heart of the harness. For each configuration:

1. Reset the ``RuntimeConfig`` singleton (critical for ``cross_encoder_enabled``
   / ``vector_weight`` / ``rrf_k`` which route through RuntimeConfig.get).
2. Clone the base ``Settings`` with per-config flag overrides + eval-DB
   connection overrides, disabling background handlers that would otherwise
   cascade writes.
3. Construct a fresh ``Database``, ``EmbeddingProvider``, ``Heart``, and
   ``Brain`` bound to the eval DB.
4. For each qrel, call :func:`nous.api.retrieval_pipeline.run_recall_pipeline`.
5. Score the retrieved IDs against ``qrel.gold_ids``; per-qrel exceptions
   are captured into ``QrelResult.error`` (never zero-scored silently).
6. Tear everything down; start the next config with a clean singleton.

The harness never touches the main Nous DB during a run.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator
from uuid import UUID

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.eval.config import EvalSettings
from nous.heart.heart import Heart
from nous.runtime_config import RuntimeConfig
from nous.storage.database import Database

if TYPE_CHECKING:
    from nous.eval.qrels_loader import Qrel

logger = logging.getLogger(__name__)


# Settings fields disabled on every eval-DB Settings clone. These are the
# background-handler + derived-pipeline flags that could cascade writes to
# the eval DB if left on. Gated through hasattr() so this list stays robust
# to the Infra subagent landing its env-var additions at a different time —
# if a field isn't declared on Settings yet, we skip it silently rather than
# raising. Each name is paired with the disabled value we want to impose.
_EVAL_DISABLE_FIELDS: tuple[tuple[str, Any], ...] = (
    ("event_bus_enabled", False),
    ("fact_extraction_enabled", False),
    ("episode_summary_enabled", False),
    ("sleep_enabled", False),
    ("actionability_backfill_on_startup", False),
    ("actionability_enabled", False),
    ("heartbeat_enabled", False),
    ("schedule_enabled", False),
    ("subtask_enabled", False),
    ("dag_enabled", False),
    ("decision_review_enabled", False),
    ("correction_extraction_enabled", False),
    ("graph_backfill_enabled", False),
    ("rubric_outcome_detection_enabled", False),
)


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalConfig:
    """A named config row in the eval matrix.

    ``flags`` maps to ``Settings`` field names that ``model_copy`` merges
    onto the base Settings. Unknown keys fail loudly (pydantic's model_copy
    with ``update=`` validates) — typos in config names don't silently
    produce the default config.
    """

    name: str
    flags: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class QrelResult:
    """Scored outcome of one qrel under one config.

    ``rank_of_first_gold`` is 1-based; ``None`` means no gold in top-K.
    ``error`` populated iff the pipeline raised for this qrel — metric
    aggregation excludes these rather than zero-scoring them silently.

    ``gold_ids`` carries the qrel's gold list so ``metrics.compute_metrics``
    can compute P@K / R@K / nDCG without going back to the original Qrel.
    Defaults to ``[]`` for callers that construct QrelResult by hand in
    tests where only rank-based metrics are exercised.
    """

    qrel_index: int
    qrel_query: str
    qrel_source: str
    retrieved_ids: list[UUID]
    retrieved_types: list[str]
    rank_of_first_gold: int | None
    n_gold_in_top_k: int
    n_gold_total: int
    error: str | None = None
    gold_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class RunResult:
    """All qrel outcomes for one config."""

    config: RetrievalConfig
    per_qrel: list[QrelResult]
    duration_seconds: float
    pipeline_stats_summary: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_matrix(
    configs: list[RetrievalConfig],
    qrels: list["Qrel"],
    eval_settings: EvalSettings,
    main_settings_template: Settings,
    top_k: int = 10,
) -> list[RunResult]:
    """Iterate configs × qrels, returning per-config ``RunResult``.

    Each config gets a fresh Database + Heart + Brain bound to the eval DB,
    with the configured feature flags applied via Settings override. The
    ``RuntimeConfig`` singleton is reset between configs so per-config
    flag changes actually propagate (otherwise the previous config's
    ``_overrides`` would shadow the new Settings values for the three
    RuntimeConfig-resolved knobs).

    Args:
        configs: List of RetrievalConfig rows (one per matrix column).
        qrels: List of Qrel rows (one per matrix row).
        eval_settings: EvalSettings for the eval DB connection + agent_id.
        main_settings_template: Production ``Settings`` used as the base for
            per-config overrides — typically a freshly constructed Settings
            instance so env vars are captured consistently.
        top_k: Retrieval depth applied uniformly across the matrix.

    Returns:
        A list of RunResult in the same order as ``configs``.
    """
    results: list[RunResult] = []
    for cfg in configs:
        RuntimeConfig.reset()
        logger.info("F051: running config=%s (%d qrels)", cfg.name, len(qrels))
        overridden = _apply_config_flags(main_settings_template, cfg)
        eval_scoped = _settings_for_eval_db(eval_settings, overridden)

        t0 = time.monotonic()
        eval_db = Database(eval_scoped)
        try:
            await eval_db.connect()
        except Exception as exc:
            logger.exception(
                "F051: Database.connect failed for config=%s", cfg.name
            )
            # Surface a synthetic QrelResult for each qrel so downstream
            # metrics know this config produced nothing.
            results.append(
                RunResult(
                    config=cfg,
                    per_qrel=[
                        _errored_qrel_result(idx, q, f"db_connect_failed: {exc}")
                        for idx, q in enumerate(qrels)
                    ],
                    duration_seconds=time.monotonic() - t0,
                )
            )
            continue

        try:
            async with _build_heart_for_eval(eval_db, eval_scoped) as heart:
                brain = _build_brain_for_eval(eval_db, eval_scoped, heart._embeddings)
                per_qrel: list[QrelResult] = []
                stats_totals: dict[str, int] = {
                    "graph_expansion_used": 0,
                    "spreading_activation_used": 0,
                    "contradiction_checks_ran": 0,
                }
                for idx, qrel in enumerate(qrels):
                    qr, ran_flags = await _run_one(
                        heart, brain, eval_scoped, qrel, idx, top_k
                    )
                    per_qrel.append(qr)
                    for k, v in ran_flags.items():
                        if v:
                            stats_totals[k] = stats_totals.get(k, 0) + 1
                duration = time.monotonic() - t0
                results.append(
                    RunResult(
                        config=cfg,
                        per_qrel=per_qrel,
                        duration_seconds=duration,
                        pipeline_stats_summary=stats_totals,
                    )
                )
                logger.info(
                    "F051: config=%s complete (%.1fs, %d/%d qrels)",
                    cfg.name,
                    duration,
                    len(per_qrel),
                    len(qrels),
                )
        finally:
            await eval_db.engine.dispose()

    return results


# ---------------------------------------------------------------------------
# Settings assembly
# ---------------------------------------------------------------------------


def _apply_config_flags(
    base: Settings, cfg: RetrievalConfig
) -> Settings:
    """Apply ``cfg.flags`` onto a Settings copy, filtering unknown keys.

    Unknown keys are dropped with a WARNING rather than raising. A flag
    that isn't declared on Settings may reflect:

    - A typo in ``configs.yaml`` (bad — but a WARNING surfaces it).
    - A feature flag that hasn't landed yet (e.g. F050's
      ``query_expansion_enabled``) — the harness should still run
      ``baseline`` cleanly while we wait for the feature.
    - A test fixture using a stub Settings without the full field set.

    Dropping-with-WARN covers all three cases; strict raise only helps
    the first and blocks the other two.
    """
    known = {k for k in cfg.flags if hasattr(base, k)}
    unknown = set(cfg.flags) - known
    if unknown:
        logger.warning(
            "F051: config %s references Settings fields not present on "
            "the base: %s — skipping those flags",
            cfg.name,
            sorted(unknown),
        )
    update = {k: cfg.flags[k] for k in known}
    if not update:
        # Support stubs that don't implement model_copy with empty update.
        try:
            return base.model_copy(update={})
        except Exception:
            return base
    return base.model_copy(update=update)


def _settings_for_eval_db(
    eval_settings: EvalSettings, base: Settings
) -> Settings:
    """Clone production Settings but swap DB connection + disable handlers.

    The disable list is filtered through ``hasattr(Settings, name)`` so the
    Core agent doesn't need to wait on the Infra agent to finish landing
    env-var declarations — a missing field is silently skipped.
    """
    update: dict[str, Any] = {
        "db_host": eval_settings.db_host,
        "db_port": eval_settings.db_port,
        "db_user": eval_settings.db_user,
        "db_password": eval_settings.db_password,
        "db_name": eval_settings.db_name,
        "db_pool_size": eval_settings.db_pool_size,
        "db_max_overflow": eval_settings.db_max_overflow,
        "agent_id": eval_settings.agent_id,
    }
    for field_name, disabled_value in _EVAL_DISABLE_FIELDS:
        if hasattr(base, field_name):
            update[field_name] = disabled_value
    return base.model_copy(update=update)


# ---------------------------------------------------------------------------
# Component construction
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _build_heart_for_eval(
    db: Database, settings: Settings
) -> AsyncIterator[Heart]:
    """Construct a Heart bound to the eval DB, closing on exit.

    Uses ``async with`` so the embedding provider's httpx client is always
    closed, even on test failure. Background tasks (EventBus, sleep handler,
    heartbeat) are not started — those belong to :mod:`nous.main`, not to
    the eval harness.
    """
    embedding_provider: EmbeddingProvider | None = None
    if settings.openai_api_key:
        embedding_provider = EmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    heart = Heart(
        database=db,
        settings=settings,
        embedding_provider=embedding_provider,
        owns_embeddings=True,
    )
    try:
        async with heart:
            yield heart
    finally:
        # Heart's __aexit__ calls close(); belt-and-suspenders in case of
        # embedding_provider construction failure before the yield.
        pass


def _build_brain_for_eval(
    db: Database,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None,
) -> Brain:
    """Construct a Brain bound to the eval DB.

    Brain and Heart share the embedding provider (pattern from main.py:69)
    so we don't double up httpx pools.
    """
    return Brain(
        database=db,
        settings=settings,
        embedding_provider=embedding_provider,
    )


# ---------------------------------------------------------------------------
# Single-qrel scoring
# ---------------------------------------------------------------------------


async def _run_one(
    heart: Heart,
    brain: Brain,
    settings: Settings,
    qrel: "Qrel",
    idx: int,
    top_k: int,
) -> tuple[QrelResult, dict[str, bool]]:
    """Run the pipeline for one qrel and score against gold.

    Returns (result, pipeline_flags) where pipeline_flags is a dict of
    which stages fired — used by the caller to increment matrix-wide counters.
    """
    memory_types: list[str] | None = None
    if qrel.memory_types:
        memory_types = [mt for mt in qrel.memory_types]

    try:
        pipeline_results, stats = await run_recall_pipeline(
            query=qrel.query,
            heart=heart,
            brain=brain,
            settings=settings,
            limit=top_k,
            memory_types=memory_types,
        )
    except Exception as exc:
        # Per plan silent-failure table: captured, not zero-scored.
        logger.exception(
            "F051: pipeline raised for qrel %d (%r)", idx, qrel.query[:80]
        )
        return (
            QrelResult(
                qrel_index=idx,
                qrel_query=qrel.query,
                qrel_source=qrel.source.value,
                gold_ids=list(qrel.gold_ids),
                retrieved_ids=[],
                retrieved_types=[],
                rank_of_first_gold=None,
                n_gold_in_top_k=0,
                n_gold_total=len(qrel.gold_ids),
                error=f"{type(exc).__name__}: {exc}",
            ),
            {},
        )

    retrieved_ids = [r.id for r in pipeline_results]
    retrieved_types = [r.type for r in pipeline_results]
    rank, n_in_top = _score_rank(retrieved_ids, qrel.gold_ids, top_k)

    logger.debug(
        "F051: qrel %d rank=%s n_gold_in_topk=%d/%d",
        idx,
        rank,
        n_in_top,
        len(qrel.gold_ids),
    )

    return (
        QrelResult(
            qrel_index=idx,
            qrel_query=qrel.query,
            qrel_source=qrel.source.value,
            gold_ids=list(qrel.gold_ids),
            retrieved_ids=retrieved_ids,
            retrieved_types=retrieved_types,
            rank_of_first_gold=rank,
            n_gold_in_top_k=n_in_top,
            n_gold_total=len(qrel.gold_ids),
            error=None,
        ),
        {
            "graph_expansion_used": stats.graph_expansion_used,
            "spreading_activation_used": stats.spreading_activation_used,
            "contradiction_checks_ran": stats.contradiction_checks_ran,
        },
    )


def _score_rank(
    retrieved: list[UUID],
    gold: list[UUID],
    top_k: int,
) -> tuple[int | None, int]:
    """Return (rank_of_first_gold_in_topk, count_of_gold_in_topk).

    Rank is 1-based. ``None`` when no gold appears in ``retrieved[:top_k]``.
    """
    gold_set = set(gold)
    top_slice = retrieved[:top_k]
    first_rank: int | None = None
    n_hits = 0
    for i, rid in enumerate(top_slice, start=1):
        if rid in gold_set:
            n_hits += 1
            if first_rank is None:
                first_rank = i
    return first_rank, n_hits


def _errored_qrel_result(idx: int, qrel: "Qrel", error: str) -> QrelResult:
    """Build a zero-retrieval QrelResult carrying an error tag.

    Used when the config-level setup (DB connect) fails — we still produce
    one QrelResult per qrel so downstream matrix code doesn't need special
    cases for empty results.
    """
    return QrelResult(
        qrel_index=idx,
        qrel_query=qrel.query,
        qrel_source=qrel.source.value,
        gold_ids=list(qrel.gold_ids),
        retrieved_ids=[],
        retrieved_types=[],
        rank_of_first_gold=None,
        n_gold_in_top_k=0,
        n_gold_total=len(qrel.gold_ids),
        error=error,
    )
