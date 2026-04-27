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
    type: Literal["episode", "fact", "procedure", "censor", "decision"]
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

    # Stage 3: Brain decision results
    decision_results: list["DecisionSummary"] = field(default_factory=list)

    # Stage 4: graph-expanded decisions (1-hop neighbors OR spreading activation)
    graph_expanded: list["NeighborResult"] = field(default_factory=list)

    # Stage 5: contradiction edges (source_id, source_type, target_id, target_type)
    contradictions: list[tuple[UUID, str, UUID, str]] = field(default_factory=list)

    # Flags
    searched_decisions: bool = False
    searched_heart: bool = False
    spreading_activation_used: bool = False
    graph_expansion_used: bool = False
    contradiction_checks_ran: bool = False

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

    Returns:
        ``(results, stats)`` where ``results`` is a flat list of
        ``PipelineResult`` ordered by stage (heart -> heart_graph ->
        decisions -> graph_expanded), and ``stats`` exposes which stages
        fired. Contradiction edges are surfaced via ``stats.contradiction_checks_ran``
        plus the per-result ``contradicts`` field.
    """
    acc = await _run_stages(query, heart, brain, settings, limit, memory_types, residual_activations)

    # Build flat PipelineResult list in stage order
    results: list[PipelineResult] = []
    results.extend(_heart_results_to_pipeline(acc.heart_results))
    results.extend(_heart_graph_to_pipeline(acc.heart_graph_decisions, settings))
    results.extend(_decisions_to_pipeline(acc.decision_results))
    results.extend(_graph_expanded_to_pipeline(acc.graph_expanded, settings))

    # Attach contradiction links to matching results
    if acc.contradictions:
        _attach_contradictions(results, acc.contradictions)

    stats = PipelineStats(
        ce_reranked=False,  # CE rerank happens inside heart.recall already
        mmr_applied=False,  # MMR happens inside heart.recall already
        graph_expansion_used=acc.graph_expansion_used,
        spreading_activation_used=acc.spreading_activation_used,
        contradiction_checks_ran=acc.contradiction_checks_ran,
        n_heart_results=len(acc.heart_results),
        n_brain_results=len(acc.decision_results),
        n_graph_expanded=len(acc.graph_expanded),
        n_stage_errors=dict(acc.stage_errors),
        contradiction_edges=list(acc.contradictions),
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
            )
            acc.heart_results = list(heart_results or [])

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
                    neighbors = await brain.neighbors(
                        hr.id,
                        node_type=hr.type,
                        limit=2,
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
    return [
        PipelineResult(
            id=r.id,
            type=r.type,
            description=r.summary,
            score=r.score,
            source="heart",
        )
        for r in heart_results
    ]


def _heart_graph_to_pipeline(
    heart_graph: list["NeighborResult"], settings: "Settings"
) -> list[PipelineResult]:
    decay = settings.graph_recall_decay
    return [
        PipelineResult(
            id=n.id,
            type="decision",  # Only decision neighbors are appended at this stage
            description=n.description,
            score=n.edge_weight * decay,
            source="graph_expanded",
            edge_relation=n.edge_relation,
        )
        for n in heart_graph
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
            score=n.edge_weight * decay,
            source=(
                "spreading_activation"
                if n.edge_relation == "spreading_activation"
                else "graph_expanded"
            ),
            edge_relation=n.edge_relation,
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
