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
from nous_eval.config import EvalSettings
from nous.heart.heart import Heart
from nous.runtime_config import RuntimeConfig
from nous.storage.database import Database

if TYPE_CHECKING:
    from nous_eval.qrels_loader import Qrel

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
    # F051.4: disable F050 query expansion in eval by default. Each recall
    # would otherwise burn 1 Haiku call; multi-turn-replay walks 600+
    # recalls per matrix run = ~$0.30 + non-determinism poisoning the
    # gate signal. Operators who explicitly want F050+F055 interaction
    # measurement can override via a config that sets it true.
    ("query_expansion_enabled", False),
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

    N7: ``retrieved_ids`` is the FULL served list, not a top-K slice — the
    pipeline returns every row it will hand the model (median ~77), and
    ``recall_deep`` does not truncate. Only ``rank_of_first_gold`` /
    ``n_gold_in_top_k`` are top-K scoped. ``retrieved_legs`` labels each
    row's originating leg so ``metrics.leg_visibility`` can tell which legs
    band below the eval's cutline and are therefore unmeasurable at fixed k.
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
    retrieved_legs: list[str] = field(default_factory=list)
    # N1/codex-P1: per-stage failure counts from PipelineStats.n_stage_errors,
    # including Heart's per-leg failures ("heart_recall_fact" etc). A non-empty
    # dict means this qrel's metrics are based on a PARTIAL retrieval — the
    # numbers still look plausible, which is exactly the schema-lag scenario
    # the N1 instrumentation exists to expose. Kept per-qrel (rather than
    # setting ``error``) so the run is not silently dropped from aggregates:
    # the failure is reported, not hidden and not zero-scored.
    stage_errors: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    """All qrel outcomes for one config."""

    config: RetrievalConfig
    per_qrel: list[QrelResult]
    duration_seconds: float
    pipeline_stats_summary: dict[str, int] = field(default_factory=dict)
    # N7/codex-R5: legs this config had ENABLED, derived from the settings
    # actually used for the run. A leg that emits zero rows never appears in
    # any QrelResult.retrieved_legs, so without this the visibility report
    # would omit a fully-silent leg instead of showing it at 0.00.
    expected_legs: list[str] = field(default_factory=list)


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
                        # bool is a subclass of int — check it FIRST, or the
                        # stage flags would be summed as 1/0 instead of
                        # counted as occurrences. Booleans count qrels;
                        # integer stage-error counters accumulate.
                        if isinstance(v, bool):
                            if v:
                                stats_totals[k] = stats_totals.get(k, 0) + 1
                        else:
                            stats_totals[k] = stats_totals.get(k, 0) + int(v)
                duration = time.monotonic() - t0
                results.append(
                    RunResult(
                        config=cfg,
                        per_qrel=per_qrel,
                        duration_seconds=duration,
                        pipeline_stats_summary=stats_totals,
                        expected_legs=_expected_legs(eval_scoped),
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

    # Some Settings fields are read via `RuntimeConfig.get()` from a freshly
    # constructed `Settings()` (see nous/heart/search.py:_resolve_vector_weight
    # / _resolve_rrf_k), which means a `model_copy(update=...)` here would be
    # silently ignored — the resolver builds its own Settings from env vars
    # and never sees our override. Push these into RuntimeConfig directly so
    # the resolver's `RuntimeConfig.get().get_*()` returns the eval value.
    # `RuntimeConfig.reset()` is called per-config in `run_matrix`, so no leak.
    if "vector_weight" in update:
        RuntimeConfig.get().set_vector_weight(float(update["vector_weight"]))
    if "rrf_k" in update:
        RuntimeConfig.get().set_rrf_k(int(update["rrf_k"]))

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

    Pre-flight: asserts the eval DB has every column the ORM expects.
    Without this, missing migrations cascade into asyncpg
    InFailedSQLTransactionError mid-query and the eval reports something
    like "0% sufficient" with no surface signal that the schema is the
    problem (see PR #398 for the cascade fix).
    """
    from nous_eval.schema_preflight import assert_eval_db_schema_matches_orm
    await assert_eval_db_schema_matches_orm(db)

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

    # F050: wire QueryExpander into Heart when query_expansion_enabled=True.
    # Mirrors nous/main.py:108-130 — without this wiring, the harness's
    # f050_on config flag is a no-op (heart._query_expander stays None).
    # We construct an HttpxAnthropicClient locally rather than reusing
    # main.py's shared one because the harness lifecycle is one-shot.
    api_client = None
    if settings.query_expansion_enabled:
        try:
            from nous.api.anthropic_client import create_client
            from nous.heart.query_expansion import QueryExpander
            api_client = create_client(settings)
            await api_client.start()
            heart.set_query_expander(QueryExpander(
                llm=api_client,
                settings=settings,
                db=db,
                model=settings.query_expansion_model,
            ))
        except Exception:
            logger.warning(
                "F050: harness QueryExpander wiring failed; f050_on collapses to baseline",
                exc_info=True,
            )

    # F055: wire ResidualActivator when residual_activation_enabled=True.
    # Mirrors nous/main.py:132-140. Without this wiring,
    # heart._residual_activator stays None and the f055_on config collapses
    # to baseline silently (sibling-of-#354 silent-pipeline-mismatch).
    if getattr(settings, "residual_activation_enabled", False):
        try:
            from nous.heart.residual_activation import ResidualActivator
            heart.set_residual_activator(ResidualActivator(
                settings=settings,
                wm=heart.working_memory,
                db=db,
            ))
            logger.info("F055: harness ResidualActivator wired for eval")
        except Exception:
            logger.warning(
                "F055: harness ResidualActivator wiring failed; f055_on collapses to baseline",
                exc_info=True,
            )

    # F377: wire the dedup-tiebreaker LLM client onto heart.facts when enabled.
    # Mirrors nous/main.py:192. Without this the fact_dedup_tiebreaker_enabled
    # flag is a no-op in the harness (is_distinct_fact returns None -> fail-open
    # dedup) and the dedup eval can't measure the tiebreaker. Reuse the F050
    # api_client if one was already created.
    if getattr(settings, "fact_dedup_tiebreaker_enabled", False):
        try:
            if api_client is None:
                from nous.api.anthropic_client import create_client
                api_client = create_client(settings)
                await api_client.start()
            heart.facts.set_llm_client(api_client, model=settings.contradiction_model)
            logger.info("F377: harness dedup tiebreaker LLM client wired for eval")
        except Exception:
            logger.warning(
                "F377: harness dedup tiebreaker wiring failed; tiebreaker collapses "
                "to fail-open dedup",
                exc_info=True,
            )

    try:
        async with heart:
            yield heart
    finally:
        # Heart's __aexit__ calls close(); also close any harness-owned
        # AnthropicClient created above so the per-config asyncpg/httpx
        # pools don't leak across configs in run_matrix.
        if api_client is not None:
            try:
                await api_client.close()
            except Exception:
                logger.debug("F050: api_client.close raised", exc_info=True)


def _build_brain_for_eval(
    db: Database,
    settings: Settings,
    embedding_provider: EmbeddingProvider | None,
) -> Brain:
    """Construct a Brain bound to the eval DB.

    Brain and Heart share the embedding provider (pattern from main.py:69)
    so we don't double up httpx pools.

    Schema preflight: Brain's ORM models (Decision) are validated by the
    Heart preflight at ``_build_heart_for_eval`` because both factories
    are called in the same harness invocation against the same eval DB.
    If you ever construct a Brain in a path that does NOT also build a
    Heart, call ``assert_eval_db_schema_matches_orm(db)`` at that site.
    """
    return Brain(
        database=db,
        settings=settings,
        embedding_provider=embedding_provider,
    )


# ---------------------------------------------------------------------------
# Density-eval helper (F053)
# ---------------------------------------------------------------------------


async def _build_densifier_for_eval(
    settings: Settings,
    db: Database,
    agent_id: str,
):
    """F053 — construct ``GraphDensifier`` against the eval DB.

    Mirrors the production wiring at ``nous/main.py:300`` so density_eval
    invokes the same densifier the prod sleep handler does. ``EmbeddingProvider``
    is required (raises ``RuntimeError`` if ``OPENAI_API_KEY`` is unset) since
    backfill is meaningless without embeddings.
    """
    from nous.brain.graph_densifier import GraphDensifier
    from nous.brain.graph_linker import GraphLinker
    from nous.brain.embeddings import EmbeddingProvider

    # F054 fix: EmbeddingProvider takes api_key as a string, not a Settings
    # object. The previous `EmbeddingProvider(settings)` call silently produced
    # a client whose Authorization header was `Bearer Settings(...)`, causing
    # 401 on every embed call. Same-type backfill survived (uses stored
    # embeddings) but cross-type re-embedding was broken on every density_eval
    # run between F053 merge (b258cbe) and this fix.
    if not settings.openai_api_key:
        raise RuntimeError(
            "F053 density_eval requires an embedder (set OPENAI_API_KEY)"
        )
    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    linker = GraphLinker(
        db=db,
        embedder=embedder,
        settings=settings,
        agent_id=agent_id,
    )
    return GraphDensifier(
        db=db,
        graph_linker=linker,
        embedder=embedder,
        settings=settings,
        agent_id=agent_id,
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
            # F065 follow-up (2026-05-23): merge stage outputs by score so
            # graph-expanded items can compete for the top-K window. The
            # default stage-order concatenation pins them at position 11+,
            # making every graph-touching config invisible to top-K=10
            # scoring. Without this, F065 penalty / F022 spreading /
            # F030 MMR-after-graph all produce structurally-zero deltas.
            rerank_by_score=True,
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
    retrieved_legs = [_leg_of(r) for r in pipeline_results]
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
            retrieved_legs=retrieved_legs,
            rank_of_first_gold=rank,
            n_gold_in_top_k=n_in_top,
            n_gold_total=len(qrel.gold_ids),
            error=None,
            # Real failures only — non-error diagnostics are excluded so
            # ``_stage_error_summary`` never calls a healthy run partial.
            stage_errors={
                k: v for k, v in stats.n_stage_errors.items()
                if k not in _NON_ERROR_STAGE_COUNTERS
            },
        ),
        {
            "graph_expansion_used": stats.graph_expansion_used,
            "spreading_activation_used": stats.spreading_activation_used,
            "contradiction_checks_ran": stats.contradiction_checks_ran,
            # N1/codex-P1: integer counters, summed (not counted) by the
            # caller. Without these, run_matrix discarded every stage failure
            # and the report showed plausible metrics with no sign that a leg
            # had crashed.
            **{
                f"stage_error_{k}": v
                for k, v in stats.n_stage_errors.items()
                if k not in _NON_ERROR_STAGE_COUNTERS
            },
            # Non-failure diagnostics live in the same dict upstream but must
            # NOT reach the partial-run banner (see _NON_ERROR_STAGE_COUNTERS).
            **{
                f"stage_info_{k}": v
                for k, v in stats.n_stage_errors.items()
                if k in _NON_ERROR_STAGE_COUNTERS
            },
        },
    )


def _expected_legs(settings: Settings) -> list[str]:
    """N7/codex-R5: legs enabled for this run, whether or not they emit.

    Derived from the master flags rather than from observed rows, because
    the whole point is to name legs that produced NOTHING. ``heart_primary``
    is unconditional. Flags are read defensively so a Settings object
    missing a newer field degrades to "not enabled" instead of raising.
    """
    legs = ["heart_primary"]
    if getattr(settings, "episode_chunks_enabled", False):
        legs.append("chunk")
    if getattr(settings, "keyed_fact_leg_enabled", False):
        legs.append("keyed")
        if int(getattr(settings, "keyed_fact_leg_rounds", 1) or 1) >= 2:
            legs.append("keyed_r2")
    if getattr(settings, "exemplar_mode_enabled", False):
        legs.append("exemplar")

    # Every graph-derived leg is NESTED under the graph master switch in the
    # pipeline, so the sub-flags alone do not mean "attempted". Listing one
    # anyway would label a deliberately disabled arm as enabled-but-silent
    # and imply it failed to emit — the opposite of the honest reporting
    # this seed exists for. Mirrors retrieval_pipeline.py:980-982 (Stage 2b)
    # and :1173 (spreading).
    graph_on = bool(getattr(settings, "graph_recall_enabled", False))
    if graph_on:
        legs.extend(("heart_graph", "brain_graph"))
        if getattr(settings, "heart_graph_all_types_enabled", False) and getattr(
            settings, "cross_type_linking_enabled", False
        ):
            legs.append("heart_graph_memory")
        # spreading_activation_enabled is "auto" | "true" | "false" (str);
        # "auto" resolves at runtime against graph density, so anything
        # other than an explicit "false" counts as attempted.
        mode = str(
            getattr(settings, "spreading_activation_enabled", "false")
        ).lower()
        if mode != "false":
            legs.append("spreading_activation")
    return legs


# Keys that live in ``PipelineStats.n_stage_errors`` but are NOT failures.
# ``heart_graph_memory_duplicates`` is deliberate corroboration telemetry —
# retrieval_pipeline.py:1131-1140 counts it to distinguish "graph found
# nothing new" from "graph corroborated a direct hit", and its own comment
# calls the latter "signal, not noise". Treating it as an error would mark
# every healthy graph-enabled eval as a partial run and declare its metrics
# invalid for comparison, which is worse than no banner at all: a warning
# that fires on success trains operators to ignore it.
_NON_ERROR_STAGE_COUNTERS: frozenset[str] = frozenset({
    "heart_graph_memory_duplicates",
})


def _leg_of(r) -> str:
    """N7: label a PipelineResult with the leg that produced it.

    Reads the provenance markers the pipeline already sets — no new
    instrumentation. ``metadata["retrieval_leg"]`` covers the F085 keyed
    rounds and the F086 exemplar leg; ``source`` covers spreading activation
    and the brain decision leg; ``metadata["stage_origin"]`` covers the
    remaining graph stages; ``type == "chunk"`` identifies the F067 chunk
    leg. Rows with no marker are plain heart hits ("heart_primary").

    ORDER IS LOAD-BEARING. ``_graph_expanded_to_pipeline`` sets BOTH
    ``source="spreading_activation"`` AND a ``stage_origin`` of
    ``brain_graph``/``heart_graph_memory`` on every spreading row
    (``retrieval_pipeline.py:2057-2077``). Checking ``stage_origin`` first
    means the label ``spreading_activation`` is never emitted and those rows
    are silently folded into a graph leg's statistics — which would leave
    N7 unable to say whether the spreading arm reached the scoring window,
    for one of the very legs this report exists to measure. So the
    spreading check runs BEFORE ``stage_origin``.
    """
    meta = getattr(r, "metadata", None) or {}
    leg = meta.get("retrieval_leg")
    if leg:
        return str(leg)
    source = getattr(r, "source", "heart")
    if source == "spreading_activation":
        return "spreading_activation"
    origin = meta.get("stage_origin")
    if origin:
        return str(origin)
    if source and source != "heart":
        return str(source)
    if getattr(r, "type", None) == "chunk":
        return "chunk"
    return "heart_primary"


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
