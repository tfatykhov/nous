"""Pure retrieval pipeline extracted from `recall_deep` (F051 Phase 1).

This module owns the full recall orchestration that used to live inside
``nous.api.tools.recall_deep``:

- Heart memory search (episodes, facts, procedures, censors)
- F022 Phase 2 cross-type graph expansion (Heart results -> decision neighbors)
- Brain decision query
- F022 graph expansion with 1-hop fallback
- F022 Phase 4 spreading activation (density-gated, CTE-based multi-hop)
- F022 Phase 3 contradiction detection (GraphEdge 'contradicts' scan)

The function returns structured ``PipelineResult`` objects + ``PipelineStats``
so callers can either:

- Format them for display (``nous.api.tools.recall_deep`` -> LLM tool output)
- Score them against qrels (``nous_eval.retrieval_runner`` -> F051 harness)

Byte-identical text output is a hard invariant of the refactor; see
``tests/fixtures/recall_deep_text_snapshot.txt`` + the matching snapshot test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select

if TYPE_CHECKING:
    from nous.brain.brain import Brain
    from nous.brain.schemas import DecisionSummary, NeighborResult
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.heart.schemas import RecallResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    """Structured result from the full recall pipeline.

    Unlike the text output of ``recall_deep``, this is machine-consumable.
    Callers can format it for LLM display OR score it against qrels.

    ``metadata`` carries type-specific fields the formatter needs to
    reconstruct the legacy text output without re-querying:
    decisions surface ``category``, ``stakes``, ``confidence``.
    """

    id: UUID
    type: Literal["episode", "fact", "procedure", "censor", "decision", "chunk"]
    description: str
    score: float
    source: Literal[
        "heart", "brain", "graph_expanded", "spreading_activation"
    ] = "heart"
    edge_relation: str | None = None
    contradicts: list[UUID] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineStats:
    """Metadata about which stages fired, for report diagnostics."""

    ce_reranked: bool = False
    mmr_applied: bool = False
    graph_expansion_used: bool = False
    spreading_activation_used: bool = False
    contradiction_checks_ran: bool = False
    # F067 observability: True iff the chunk-recall stage was eligible
    # AND attempted. Mirrors ``_PipelineAccumulator.chunks_searched`` —
    # surfaced here so callers (recall_deep logger, eval harness) can
    # distinguish "feature flag off / ineligible memory_types" from
    # "stage ran but produced 0 chunks in top-K".
    chunks_searched: bool = False
    n_heart_results: int = 0
    n_brain_results: int = 0
    n_graph_expanded: int = 0
    # Per-stage error counts surfaced to eval reports. Keys are pipeline
    # stage names: "heart_recall", "brain_query", "heart_graph_neighbors",
    # "decision_neighbors", "spreading_activation", "contradiction_query".
    # Heart's INTERNAL per-sub-search exceptions (heart.py:807-812) are NOT
    # included here — they are caught and logged at WARN inside Heart.recall
    # before the pipeline sees results. Surfacing those would require Heart
    # instrumentation; deferred to a follow-up.
    n_stage_errors: dict[str, int] = field(default_factory=dict)
    # Raw contradiction edges (source_id, source_type, target_id, target_type).
    # Preserves byte-identical warning emission for the legacy formatter
    # because PipelineResult.contradicts deduplicates and loses source-type
    # information.
    contradiction_edges: list[tuple[UUID, str, UUID, str]] = field(
        default_factory=list
    )
    # F071: count of results dropped because their id was in the current turn's
    # system prompt (cross-context dedup). 0 when feature flag is off / no
    # exclusion set was passed. Surfaced in recall_deep INFO log so the
    # duplication-tax measurement is grep-able from prod.
    excluded_in_context: int = 0


# ---------------------------------------------------------------------------
# Internal accumulator (mutable during the pipeline; frozen stats are emitted at end)
# ---------------------------------------------------------------------------


@dataclass
class _PipelineAccumulator:
    """Mutable staging area for a single pipeline run.

    We build up groups of results keyed by stage because the current text
    formatter needs to emit them in stage order (Heart section, Graph-Connected
    Decisions section, Brain Decisions section). Returning a flat list plus
    stats is enough for the eval harness; the formatter re-groups via
    ``source``.
    """

    # Stage 1: Heart recall results (raw RecallResult objects preserved for fidelity)
    heart_results: list["RecallResult"] = field(default_factory=list)
    heart_types_searched: list[str] = field(default_factory=list)

    # Stage 2: cross-type graph neighbors from Heart seeds (F022 Phase 2)
    heart_graph_decisions: list["NeighborResult"] = field(default_factory=list)
    # Stage 2b: non-decision graph neighbors from Heart/chunk seeds.
    # Path A (heart_graph_all_types_enabled): when set, this collects
    # fact/episode/chunk/procedure neighbors that previously had no consumer.
    # Kept distinct from heart_graph_decisions so the existing
    # decision-focused stage's downstream formatter/scorer shape is
    # unchanged when the flag is off.
    heart_graph_memory_neighbors: list["NeighborResult"] = field(default_factory=list)

    # Stage 3: Brain decision results
    decision_results: list["DecisionSummary"] = field(default_factory=list)

    # Stage 4: graph-expanded decisions (1-hop neighbors OR spreading activation)
    graph_expanded: list["NeighborResult"] = field(default_factory=list)

    # Stage 5: contradiction edges (source_id, source_type, target_id, target_type)
    contradictions: list[tuple[UUID, str, UUID, str]] = field(default_factory=list)

    # F067 Stage 1.5: chunk-recall results
    # Shape: (id, content, score, episode_id) — see _search_episode_chunks.
    chunk_results: list[tuple[UUID, str, float, UUID]] = field(default_factory=list)

    # Flags
    searched_decisions: bool = False
    searched_heart: bool = False
    spreading_activation_used: bool = False
    graph_expansion_used: bool = False
    contradiction_checks_ran: bool = False
    chunks_searched: bool = False

    # Per-stage error counter — incremented when a try/except around a stage
    # call catches an exception. Surfaced via PipelineStats.n_stage_errors.
    stage_errors: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_recall_pipeline(
    query: str,
    heart: "Heart",
    brain: "Brain",
    settings: "Settings",
    limit: int = 10,
    memory_types: list[str] | None = None,
    residual_activations: dict[UUID, float] | None = None,
    apply_mmr: bool | None = None,
    rerank_by_score: bool = False,
    exclude_ids: dict[str, set[str]] | None = None,
) -> tuple[list[PipelineResult], PipelineStats]:
    """Run the full retrieval pipeline.

    This is what the production ``recall_deep`` tool runs, but returning
    structured results instead of formatted text.

    Args:
        query: Search query string.
        heart: Heart instance (owns episodes/facts/procedures/censors).
        brain: Brain instance (owns decisions + graph edges).
        settings: Settings controlling the feature flags
            (``graph_recall_enabled``, ``cross_type_linking_enabled``,
            ``spreading_activation_enabled``, ``contradiction_detection``,
            ``graph_recall_decay``, ``graph_recall_max_expand``,
            ``graph_recall_max_neighbors``).
        limit: Max results per sub-search.
        memory_types: Types to search (``episode, fact, procedure, censor,
            decision``). ``None`` or containing ``"all"`` searches everything.
        apply_mmr: F030.2 per-consumer MMR override. None=settings-driven,
            True=force MMR (bypass skip_after_ce), False=force MMR off.

    Returns:
        ``(results, stats)`` where ``results`` is a flat list of
        ``PipelineResult`` ordered by stage (heart -> heart_graph ->
        decisions -> graph_expanded), and ``stats`` exposes which stages
        fired. Contradiction edges are surfaced via ``stats.contradiction_checks_ran``
        plus the per-result ``contradicts`` field.
    """
    acc = await _run_stages(
        query, heart, brain, settings, limit, memory_types,
        residual_activations, apply_mmr=apply_mmr,
    )

    # Build flat PipelineResult list in stage order
    results: list[PipelineResult] = []
    results.extend(_heart_results_to_pipeline(acc.heart_results))
    # F067: episode chunks appended to Heart section (same source tag,
    # naturally co-displayed). Empty when feature flag is off.
    results.extend(_chunks_to_pipeline(acc.chunk_results))
    results.extend(_heart_graph_to_pipeline(acc.heart_graph_decisions, settings))
    # Path A: non-decision neighbors land in the same stage-order slot as the
    # decision-only graph results so subsequent ``rerank_by_score`` reorders
    # them uniformly with everything else.
    results.extend(_heart_graph_memory_to_pipeline(
        acc.heart_graph_memory_neighbors, settings,
    ))
    results.extend(_decisions_to_pipeline(acc.decision_results))
    # P1.1: batch-resolve source_episode_id for fact-type results so the
    # formatter can session-group the Heart section. Episodes already carry
    # their id as the session; chunks got their episode_id from the chunk
    # search query. Facts need a follow-up lookup.
    # Codex round-5 P2: gated behind the consumer flag so the extra
    # DB round-trip doesn't run on every recall when the feature is off.
    if getattr(settings, "session_group_heart_section", False):
        results = await _attach_fact_source_episodes(heart, results)

    # P2: graph-adjacency boost (gbrain-inspired). When connected via
    # sleep-built edges (F040), candidates reinforce each other. Gated
    # by feature flag; default off.
    if getattr(settings, "graph_adjacency_boost_enabled", False):
        alpha = float(getattr(settings, "graph_adjacency_boost_alpha", 0.15))
        results = await _apply_graph_adjacency_boost(
            brain, results, alpha=alpha,
        )
    results.extend(_graph_expanded_to_pipeline(acc.graph_expanded, settings))

    # Attach contradiction links to matching results
    if acc.contradictions:
        _attach_contradictions(results, acc.contradictions)

    # F065 follow-up (2026-05-23): optional score-based merge.
    #
    # The default stage-order assembly above places graph-expanded items
    # AFTER heart_results in the concatenated list — so they always sit
    # at position 11+ for a top-K=10 reader, no matter how high their
    # scores are. That makes every graph-touching feature (F022 spreading
    # activation, F030 MMR-after-graph, F065 inferred-edge penalty)
    # invisible to a top-K=10 eval.
    #
    # When ``rerank_by_score=True``, we stably re-sort by score descending
    # (Python's sort is stable, so equal scores preserve stage order as
    # a tiebreaker). Default ``False`` keeps the `recall_deep` tool output
    # byte-identical to its committed snapshot — only callers that opt in
    # (the F051 harness today) see the new behavior. When we're confident
    # the merged ordering is preferable in production too, the default
    # can be flipped and the snapshot regenerated.
    if rerank_by_score:
        results.sort(key=lambda r: r.score or 0.0, reverse=True)

    # F071: drop results whose id is already in the system prompt for this
    # turn. Applied AFTER all scoring (rerank, MMR, CE inside heart.recall)
    # so the LLM sees the next-best alternatives — not the items below the
    # now-excluded head. Type-keyed so a UUID collision across types
    # (defensive, won't happen in practice) doesn't cross-filter.
    # Counter is per-PipelineStats (per-call), so concurrent calls don't race.
    excluded_in_context = 0
    if exclude_ids:
        before = len(results)
        results = [
            r for r in results
            if str(r.id) not in exclude_ids.get(r.type, set())
        ]
        excluded_in_context = before - len(results)

    stats = PipelineStats(
        ce_reranked=False,  # CE rerank happens inside heart.recall already
        mmr_applied=False,  # MMR happens inside heart.recall already
        graph_expansion_used=acc.graph_expansion_used,
        spreading_activation_used=acc.spreading_activation_used,
        contradiction_checks_ran=acc.contradiction_checks_ran,
        chunks_searched=acc.chunks_searched,  # F067 observability
        n_heart_results=len(acc.heart_results),
        n_brain_results=len(acc.decision_results),
        n_graph_expanded=len(acc.graph_expanded),
        n_stage_errors=dict(acc.stage_errors),
        contradiction_edges=list(acc.contradictions),
        excluded_in_context=excluded_in_context,  # F071
    )
    return results, stats


# ---------------------------------------------------------------------------
# Stage execution — mirrors nous/api/tools.py::recall_deep (pre-refactor) exactly
# ---------------------------------------------------------------------------


async def _run_stages(
    query: str,
    heart: "Heart",
    brain: "Brain",
    settings: "Settings",
    limit: int,
    memory_types: list[str] | None,
    residual_activations: dict[UUID, float] | None = None,
    apply_mmr: bool | None = None,
) -> _PipelineAccumulator:
    acc = _PipelineAccumulator()

    # Determine which types to search
    search_types = memory_types or ["all"]
    search_all = "all" in search_types

    # ------------------------------------------------------------------
    # Stage 1: Heart memory search
    # ------------------------------------------------------------------
    heart_types: list[str] = []
    if search_all or any(
        t in search_types for t in ["episode", "fact", "procedure", "censor"]
    ):
        if search_all:
            heart_types = ["episode", "fact", "procedure", "censor"]
        else:
            heart_types = [
                t
                for t in search_types
                if t in ["episode", "fact", "procedure", "censor"]
            ]

        if heart_types:
            acc.searched_heart = True
            acc.heart_types_searched = heart_types
            heart_results = await heart.recall(
                query, limit=limit, types=heart_types,
                residual_activations=residual_activations,  # F055
                apply_mmr=apply_mmr,  # F030.2
            )
            acc.heart_results = list(heart_results or [])

    # ------------------------------------------------------------------
    # Stage 1.5: F067 episode chunks (opt-in; default off)
    # ------------------------------------------------------------------
    # Chunks are searched when memory_types includes "all" or "fact"
    # (chunks are conceptually fact-adjacent and ride on the same gate as
    # facts). We deliberately do NOT gate on `"chunk" in search_types`
    # because the legacy formatter's heart_section_eligible only checks
    # episode/fact/procedure/censor — a caller passing memory_types=["chunk"]
    # would otherwise retrieve chunks that the formatter silently drops.
    if getattr(settings, "episode_chunks_enabled", False) and (
        search_all or "fact" in search_types
    ):
        # Mark BEFORE the try so PipelineStats can distinguish "flag off"
        # (chunks_searched stays False) from "flag on but stage failed"
        # (chunks_searched True + n_stage_errors["chunk_recall"] non-zero).
        acc.chunks_searched = True
        try:
            acc.chunk_results = await _search_episode_chunks(
                heart=heart,
                query=query,
                agent_id=heart.agent_id,
                limit=min(
                    settings.episode_chunk_recall_limit, limit * 2
                ),
            )
        except Exception:
            # Match the sibling stages' pattern (spreading_activation,
            # decision_neighbors): log + count. Non-eval callers (recall_deep
            # tool) discard the stats counter, so the log is the operator's
            # only signal that chunk recall is broken.
            logger.warning(
                "F067: chunk_recall failed for agent=%s query=%r "
                "(non-fatal, no chunk results this turn)",
                heart.agent_id, query[:80], exc_info=True,
            )
            acc.stage_errors["chunk_recall"] = (
                acc.stage_errors.get("chunk_recall", 0) + 1
            )

    # ------------------------------------------------------------------
    # Stage 2: F022 Phase 2 cross-type graph expansion from Heart seeds
    # ------------------------------------------------------------------
    if (
        heart_types
        and acc.heart_results
        and settings.graph_recall_enabled
        and settings.cross_type_linking_enabled
    ):
        seen_graph_ids: set[UUID] = set()
        for hr in acc.heart_results[:3]:
            if hr.type in ("fact", "episode"):
                try:
                    # F070 fix: push the decision filter into SQL so
                    # ``LIMIT 2`` returns 2 decisions, not 2 of (decisions
                    # + chunks + facts + ...) which the Python filter
                    # below would then mostly discard. With F070 adding
                    # ~37K chunk→fact summarized_by edges, the un-filtered
                    # union frequently returned 2 chunks → 0 decisions →
                    # silent decision-expansion loss.
                    neighbors = await brain.neighbors(
                        hr.id,
                        node_type=hr.type,
                        limit=2,
                        neighbor_type="decision",
                    )
                    for n in neighbors:
                        if n.node_type == "decision" and n.id not in seen_graph_ids:
                            acc.heart_graph_decisions.append(n)
                            seen_graph_ids.add(n.id)
                except Exception:
                    # Matches pre-refactor: swallow silently (see tools.py:380-381).
                    # Surface count to PipelineStats.n_stage_errors for eval visibility.
                    acc.stage_errors["heart_graph_neighbors"] = (
                        acc.stage_errors.get("heart_graph_neighbors", 0) + 1
                    )

        # ------------------------------------------------------------------
        # Stage 2b (Path A): non-decision graph neighbors from Heart seeds.
        # Path-A activates F022 cross-type / F040 / F070 edges that today
        # have no consumer beyond adjacency_boost. Each fact/episode/chunk
        # seed fans out one ``brain.neighbors`` call per target neighbor
        # type so a small ``LIMIT`` returns N rows of THAT type, not N
        # rows that the dedup below would mostly throw away. Mirrors the
        # Stage 2 SQL-pushdown fix (F070 review round 6) one floor up.
        # Flag-gated so prod retrieval shape is unchanged by default.
        # ------------------------------------------------------------------
        if settings.heart_graph_all_types_enabled:
            mem_limit = max(1, int(settings.heart_graph_neighbors_per_seed))
            seen_mem_ids: set[UUID] = set()
            heart_ids: set[UUID] = {hr.id for hr in acc.heart_results}
            # acc.chunk_results carries (id, content, score, episode_id) per
            # _search_episode_chunks at line 825. Use star-unpack so future
            # tuple shape changes don't crash this hot loop.
            chunk_ids: set[UUID] = {item[0] for item in acc.chunk_results}
            # Heart seeds: top-K fact/episode results.
            mem_seeds: list[tuple[UUID, str]] = [
                (hr.id, hr.type) for hr in acc.heart_results[:3]
                if hr.type in ("fact", "episode")
            ]
            # Chunk seeds: top-K F067 chunk-recall results (when present).
            # Chunks have rich same-episode neighborhoods via F070.
            mem_seeds.extend(
                (item[0], "chunk") for item in acc.chunk_results[:3]
            )
            # Per-type fan-out — one ``LIMIT`` window per neighbor type so
            # chunks don't crowd facts/episodes (or vice versa) out of a
            # single small union limit. Order is irrelevant; the global
            # rerank handles final ordering.
            mem_neighbor_types = ("fact", "episode", "chunk", "procedure")
            for seed_id, seed_type in mem_seeds:
                for nbr_type in mem_neighbor_types:
                    try:
                        mem_neighbors = await brain.neighbors(
                            seed_id,
                            node_type=seed_type,
                            limit=mem_limit,
                            neighbor_type=nbr_type,
                        )
                    except Exception:
                        # Mirror chunk-recall (Stage 1.5) pattern: log + count.
                        # ``stage_errors`` is discarded by prod callers
                        # (recall_deep tool), so the log is the operator's
                        # only signal that Path A is broken in prod.
                        logger.warning(
                            "Path A: heart_graph_memory_neighbors brain.neighbors "
                            "failed for agent=%s seed=%s seed_type=%s nbr_type=%s "
                            "(non-fatal, this fan-out path contributes nothing this turn)",
                            brain.agent_id, seed_id, seed_type, nbr_type,
                            exc_info=True,
                        )
                        acc.stage_errors["heart_graph_memory_neighbors"] = (
                            acc.stage_errors.get("heart_graph_memory_neighbors", 0) + 1
                        )
                        continue
                    for n in mem_neighbors:
                        # Decision neighbors are handled by Stage 2 above; even
                        # though we passed neighbor_type=nbr_type (non-decision),
                        # keep the guard for defense against future filter drift.
                        if n.node_type == "decision":
                            continue
                        if n.id in seen_mem_ids:
                            continue
                        # Skip duplicates against existing candidate pool. Track
                        # duplicates as a separate counter so eval can distinguish
                        # "graph found nothing new" from "graph corroborated a
                        # direct hit" — the latter is signal, not noise.
                        if n.id in heart_ids or n.id in chunk_ids:
                            acc.stage_errors["heart_graph_memory_duplicates"] = (
                                acc.stage_errors.get(
                                    "heart_graph_memory_duplicates", 0,
                                ) + 1
                            )
                            continue
                        acc.heart_graph_memory_neighbors.append(n)
                        seen_mem_ids.add(n.id)

    # ------------------------------------------------------------------
    # Stage 3+4: Brain decisions + graph expansion
    # ------------------------------------------------------------------
    decision_results: list["DecisionSummary"] = []
    graph_expanded: list["NeighborResult"] = []

    if search_all or "decision" in search_types:
        acc.searched_decisions = True
        decision_results = await brain.query(query, limit=limit)

        # F022: graph expansion — expand top decisions
        if decision_results and settings.graph_recall_enabled:
            seen_ids: set[UUID] = {d.id for d in decision_results}

            # F022 Phase 4: density-gated spreading activation
            use_spreading = False
            if settings.spreading_activation_enabled != "false":
                try:
                    from nous.brain.spreading_activation import (
                        compute_graph_density,
                        should_use_spreading_activation,
                    )

                    async with brain.db.session() as sa_session:
                        density = await compute_graph_density(
                            sa_session, brain.agent_id
                        )
                        use_spreading = should_use_spreading_activation(
                            settings, density
                        )
                except Exception:
                    logger.debug("Density check failed, using 1-hop")
                    acc.stage_errors["spreading_density_check"] = (
                        acc.stage_errors.get("spreading_density_check", 0) + 1
                    )

            if use_spreading:
                try:
                    from nous.brain.schemas import NeighborResult
                    from nous.brain.spreading_activation import (
                        spreading_activation_search,
                    )

                    async with brain.db.session() as sa_session:
                        seeds = [
                            (d.id, "decision", d.score or 0.5)
                            for d in decision_results[: settings.graph_recall_max_expand]
                        ]
                        activated = await spreading_activation_search(
                            sa_session, brain.agent_id, seeds, settings
                        )
                        seed_ids = {s[0] for s in seeds}
                        for nid, ntype, activation in activated:
                            if (
                                nid not in seed_ids
                                and nid not in seen_ids
                                and activation > 0.1
                            ):
                                graph_expanded.append(
                                    NeighborResult(
                                        id=nid,
                                        node_type=ntype,
                                        description=f"[{ntype}] {str(nid)[:8]}",
                                        edge_relation="spreading_activation",
                                        edge_weight=activation,
                                        created_at=datetime.now(UTC),
                                    )
                                )
                                seen_ids.add(nid)
                    acc.spreading_activation_used = True
                except Exception:
                    logger.debug(
                        "Spreading activation failed, falling back to 1-hop"
                    )
                    acc.stage_errors["spreading_activation"] = (
                        acc.stage_errors.get("spreading_activation", 0) + 1
                    )
                    use_spreading = False

            if not use_spreading:
                # Fall back to 1-hop expansion
                for dec in decision_results[: settings.graph_recall_max_expand]:
                    if dec.score is None:
                        continue
                    try:
                        neighbors = await brain.neighbors(
                            dec.id,
                            node_type="decision",
                            limit=settings.graph_recall_max_neighbors,
                        )
                        for n in neighbors:
                            if n.id not in seen_ids:
                                graph_expanded.append(n)
                                seen_ids.add(n.id)
                    except Exception:
                        logger.debug(
                            "Graph expansion failed for decision %s", dec.id
                        )
                        acc.stage_errors["decision_neighbors"] = (
                            acc.stage_errors.get("decision_neighbors", 0) + 1
                        )

            if graph_expanded:
                acc.graph_expansion_used = True

    acc.decision_results = decision_results
    acc.graph_expanded = graph_expanded

    # ------------------------------------------------------------------
    # Stage 5: F022 Phase 3 contradiction detection
    # ------------------------------------------------------------------
    if settings.graph_recall_enabled and settings.contradiction_detection:
        try:
            all_ids: set[UUID] = set()
            if acc.searched_decisions:
                for d in decision_results:
                    all_ids.add(d.id)
                for n in graph_expanded:
                    all_ids.add(n.id)

            if len(all_ids) >= 2:
                acc.contradiction_checks_ran = True
                from nous.storage.models import GraphEdge as GE

                async with brain.db.session() as cs:
                    cr = await cs.execute(
                        select(GE).where(
                            GE.relation == "contradicts",
                            GE.source_id.in_(all_ids),
                            GE.target_id.in_(all_ids),
                        )
                    )
                    for c in cr.scalars().all():
                        acc.contradictions.append(
                            (c.source_id, c.source_type, c.target_id, c.target_type)
                        )
        except Exception:
            # Matches pre-refactor: non-critical, suppressed (see tools.py:508-509).
            # Counted so eval reports surface silent contradiction-query failures.
            acc.stage_errors["contradiction_query"] = (
                acc.stage_errors.get("contradiction_query", 0) + 1
            )

    return acc


# ---------------------------------------------------------------------------
# Accumulator -> PipelineResult conversion
# ---------------------------------------------------------------------------


def _heart_results_to_pipeline(
    heart_results: list["RecallResult"],
) -> list[PipelineResult]:
    """Convert Heart RecallResults into PipelineResults.

    Forwards the full ``RecallResult.metadata`` dict so future consumers
    don't have to amend this conversion when they add new keys. Strips
    only the ``event_date`` value when it's None (F075: avoid a False-y
    placeholder that breaks ``"event_date" in metadata`` consumer checks).
    """
    out: list[PipelineResult] = []
    for r in heart_results:
        meta = dict(r.metadata) if r.metadata else {}
        # F075: omit the key entirely when None so ``in metadata``
        # presence checks correctly distinguish "no date" from "present".
        if meta.get("event_date") is None:
            meta.pop("event_date", None)
        out.append(
            PipelineResult(
                id=r.id,
                type=r.type,
                description=r.summary,
                score=r.score,
                source="heart",
                metadata=meta,
            )
        )
    return out


async def _apply_graph_adjacency_boost(
    brain: "Brain",
    results: list[PipelineResult],
    *,
    alpha: float = 0.15,
) -> list[PipelineResult]:
    """Boost candidates connected to other candidates via graph edges.

    Hypothesis (gbrain-inspired): when several retrieved items belong to
    the same semantic cluster (connected by sleep-built inferred edges,
    F040 graph densification), they reinforce each other — the cluster
    is more likely to be the right answer.

    Algorithm:
        1. Find edges in ``brain.graph_edges`` where BOTH endpoints are in
           the candidate set.
        2. Sum edge weights per candidate to get an "adjacency degree".
        3. Apply multiplicative boost: ``score *= (1 + alpha * d/max_d)``.
        4. Re-sort by boosted score.

    Fails open: any DB error returns the original list unchanged.
    """
    from dataclasses import replace
    from sqlalchemy import text as sa_text

    if not results or len(results) < 2:
        return results

    candidate_ids = [str(r.id) for r in results]
    try:
        async with brain.db.session() as s:
            # Codex round-5 P2: exclude `contradicts` so mutually
            # inconsistent candidates don't reinforce each other.
            # Aligns with the spreading-activation filter at
            # spreading_activation.py:103.
            rows = (await s.execute(sa_text(
                "SELECT source_id::text, target_id::text, weight "
                "FROM brain.graph_edges "
                "WHERE agent_id = :a "
                "  AND relation != 'contradicts' "
                "  AND source_id = ANY(CAST(:ids AS uuid[])) "
                "  AND target_id = ANY(CAST(:ids AS uuid[]))"
            ), {"a": brain.agent_id, "ids": candidate_ids})).all()
    except Exception:
        return results  # fail-open — adjacency boost is a refinement

    degree: dict[str, float] = {}
    for src, tgt, w in rows:
        w = float(w or 0.0)
        degree[src] = degree.get(src, 0.0) + w
        degree[tgt] = degree.get(tgt, 0.0) + w

    if not degree:
        return results
    max_deg = max(degree.values())
    if max_deg <= 0:
        return results

    boosted = []
    for r in results:
        d = degree.get(str(r.id), 0.0)
        boost = 1.0 + alpha * (d / max_deg)
        boosted.append(replace(r, score=(r.score or 0.0) * boost))
    boosted.sort(key=lambda r: r.score or 0.0, reverse=True)
    return boosted


async def _attach_fact_source_episodes(
    heart: "Heart", results: list[PipelineResult],
) -> list[PipelineResult]:
    """P1.1: batch-attach source_episode_id to fact-type PipelineResults.

    Used by the formatter to session-group the Heart Memory section. One
    SQL query for all fact IDs; failures (DB error, missing fact) leave
    metadata unchanged so the formatter falls back to flat listing.

    Chunks already carry source_episode_id from _search_episode_chunks.
    Episodes use their own id as the session. Procedures/decisions don't
    belong to a session — left untouched.
    """
    from dataclasses import replace
    from sqlalchemy import text as sa_text
    fact_ids = [str(r.id) for r in results if r.type == "fact"]
    if not fact_ids:
        return results
    src_map: dict[str, str | None] = {}
    try:
        async with heart.db.session() as s:
            res = await s.execute(sa_text(
                "SELECT id::text, source_episode_id::text FROM heart.facts "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND agent_id = :a"
            ), {"ids": fact_ids, "a": heart.agent_id})
            rows = res.all()
        for fid, src in rows:
            src_map[fid] = src
    except Exception:
        # Fail-open: formatter will fall back to flat listing.
        # Also catches test fixtures where heart.db.session() returns mocks
        # that don't fully implement the async context-manager protocol.
        return results

    out = []
    for r in results:
        if r.type == "fact":
            src = src_map.get(str(r.id))
            if src and not r.metadata.get("source_episode_id"):
                new_md = dict(r.metadata)
                new_md["source_episode_id"] = src
                out.append(replace(r, metadata=new_md))
            else:
                out.append(r)
        else:
            out.append(r)
    return out


def _chunks_to_pipeline(
    chunk_results: list,
) -> list[PipelineResult]:
    """F067: convert chunk-search rows into PipelineResult.

    Chunks are tagged ``type="chunk"`` and ``source="heart"`` so they appear
    in the legacy Heart Memory section of the formatter without extra logic.

    P1.1: accepts both 3-tuples ``(id, content, score)`` (legacy) and
    4-tuples ``(id, content, score, episode_id)`` (new from
    _search_episode_chunks). When episode_id is present, stash it in
    metadata so the formatter can session-group.
    """
    out = []
    for row in chunk_results:
        if len(row) >= 4:
            cid, content, score, episode_id = row[0], row[1], row[2], row[3]
            md = {"source_episode_id": episode_id} if episode_id else {}
        else:
            cid, content, score = row[0], row[1], row[2]
            md = {}
        out.append(PipelineResult(
            id=cid, type="chunk", description=content, score=score,
            source="heart", metadata=md,
        ))
    return out


async def _search_episode_chunks(
    heart: "Heart",
    query: str,
    agent_id: str,
    limit: int,
) -> list[tuple[UUID, str, float]]:
    """F067: vector-similarity search over heart.episode_chunks.

    Returns ``[(id, content, similarity_score)]`` ordered by descending
    similarity. Returns ``[]`` only when no embedder is wired (no way to
    embed the query) or the query embeds to an empty vector. All other
    failure modes — embed timeout, DB error, pgvector cast failure — RAISE
    so the caller's try/except surfaces them via ``acc.stage_errors``
    AND a WARN log. Silent fall-through to ``[]`` on embedder failure
    would masquerade as "no matches" and hide real outages.
    """
    from sqlalchemy import text as sa_text
    embedder = getattr(heart, "_embeddings", None)
    if embedder is None:
        return []
    query_vec = await embedder.embed(query)
    if not query_vec:
        return []
    vec_lit = "[" + ",".join(f"{v:.6f}" for v in query_vec) + "]"
    async with heart.db.session() as s:
        # P1.1: include episode_id so the formatter can session-group chunks.
        rows = (await s.execute(sa_text(
            "SELECT id, content, 1 - (embedding <=> CAST(:v AS vector)) AS sim, episode_id "
            "FROM heart.episode_chunks "
            "WHERE agent_id = :a AND embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:v AS vector) "
            "LIMIT :k"
        ), {"v": vec_lit, "a": agent_id, "k": limit})).all()
    return [(r[0], r[1], float(r[2]), r[3]) for r in rows]


def _f065_provenance_penalty(
    neighbor: "NeighborResult", base_score: float, decay: float, settings: "Settings"
) -> float:
    """F065: apply the inferred-edge penalty on top of F022's graph decay.

    Returns ``base_score * decay * penalty`` where ``penalty`` is
    ``settings.graph_inferred_edge_penalty`` (default 1.0 — dark launch)
    iff the neighbor's edge is `inferred`. All other tiers, plus
    spreading-activation results, pass through unchanged.

    NULL handling: ``NeighborResult.extraction_method`` defaults to
    ``'heuristic'`` at the schema, so this helper should never see None.
    The ``or "heuristic"`` is belt-and-braces (matches F065 spec NULL-handling
    rule: NULL → heuristic, fail-open).

    Spreading-activation defense in depth: SA already filters
    `relation != 'contradicts'` at spreading_activation.py:103. If a future
    inferred-tier relation bypasses the SA filter, this short-circuit keeps
    the penalty from double-applying.
    """
    if neighbor.edge_relation == "spreading_activation":
        return base_score * decay
    method = neighbor.extraction_method or "heuristic"
    penalty = settings.graph_inferred_edge_penalty if method == "inferred" else 1.0
    return base_score * decay * penalty


def _heart_graph_to_pipeline(
    heart_graph: list["NeighborResult"], settings: "Settings"
) -> list[PipelineResult]:
    decay = settings.graph_recall_decay
    return [
        PipelineResult(
            id=n.id,
            type="decision",  # Only decision neighbors are appended at this stage
            description=n.description,
            score=_f065_provenance_penalty(n, n.edge_weight, decay, settings),
            source="graph_expanded",
            edge_relation=n.edge_relation,
            # Stage origin tag — the formatter uses this to bucket
            # graph_expanded results into the "Graph-Connected Decisions"
            # (heart-side) vs "Brain Decisions" (brain-side) sections
            # without relying on positional inference. Required because
            # ``rerank_by_score`` can globally re-sort the result list,
            # which would otherwise break the position-based heuristic.
            metadata={"stage_origin": "heart_graph"},
        )
        for n in heart_graph
    ]


def _heart_graph_memory_to_pipeline(
    memory_neighbors: list["NeighborResult"], settings: "Settings"
) -> list[PipelineResult]:
    """Path A: non-decision graph-neighbor results.

    Carries the neighbor's actual ``node_type`` (fact/episode/chunk/procedure)
    so the formatter and downstream scorers can route them like any other
    memory candidate. Scoring uses the same decay + F065 provenance penalty
    pattern as the decision-neighbor path.
    """
    decay = settings.graph_recall_decay
    return [
        PipelineResult(
            id=n.id,
            type=n.node_type,
            description=n.description,
            score=_f065_provenance_penalty(n, n.edge_weight, decay, settings),
            source="graph_expanded",
            edge_relation=n.edge_relation,
            metadata={"stage_origin": "heart_graph_memory"},
        )
        for n in memory_neighbors
    ]


def _decisions_to_pipeline(
    decisions: list["DecisionSummary"],
) -> list[PipelineResult]:
    return [
        PipelineResult(
            id=d.id,
            type="decision",
            description=d.description,
            # Preserve original score (incl. None) inside metadata so the
            # formatter can reproduce the truthy-elision behavior. The
            # top-level ``score`` is normalized to 0.0 for None for callers
            # that need a numeric (e.g., the eval harness).
            score=d.score if d.score is not None else 0.0,
            source="brain",
            metadata={
                "category": d.category,
                "stakes": d.stakes,
                "confidence": d.confidence,
                "raw_score": d.score,
            },
        )
        for d in decisions
    ]


def _graph_expanded_to_pipeline(
    graph_expanded: list["NeighborResult"], settings: "Settings"
) -> list[PipelineResult]:
    decay = settings.graph_recall_decay
    return [
        PipelineResult(
            id=n.id,
            type=n.node_type,  # type: ignore[arg-type]
            description=n.description,
            score=_f065_provenance_penalty(n, n.edge_weight, decay, settings),
            source=(
                "spreading_activation"
                if n.edge_relation == "spreading_activation"
                else "graph_expanded"
            ),
            edge_relation=n.edge_relation,
            # Stage origin tag — companion to _heart_graph_to_pipeline.
            # Brain-side graph expansion (1-hop neighbors of brain seeds
            # OR spreading activation from brain seeds) ends up under the
            # "Brain Decisions" section. Keeping this metadata in sync
            # with the formatter's bucketing logic is what makes the
            # output stable under ``rerank_by_score``.
            metadata={"stage_origin": "brain_graph"},
        )
        for n in graph_expanded
    ]


def _attach_contradictions(
    results: list[PipelineResult],
    contradictions: list[tuple[UUID, str, UUID, str]],
) -> None:
    """Mutate contradicts lists on matching PipelineResults.

    PipelineResult is frozen, so we rebuild each affected entry in-place via
    index replacement. For a typical query this touches <=5 results.
    """
    by_id: dict[UUID, list[int]] = {}
    for idx, r in enumerate(results):
        by_id.setdefault(r.id, []).append(idx)

    for src_id, _src_type, tgt_id, _tgt_type in contradictions:
        for idx in by_id.get(src_id, []):
            cur = results[idx]
            new_contradicts = [*cur.contradicts, tgt_id]
            results[idx] = PipelineResult(
                id=cur.id,
                type=cur.type,
                description=cur.description,
                score=cur.score,
                source=cur.source,
                edge_relation=cur.edge_relation,
                contradicts=new_contradicts,
                metadata=cur.metadata,
            )
