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

import difflib
import logging
import re
import time
import weakref
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select

from nous.heart.exemplars import parse_label
from nous.heart.keys import extract_entity_candidates, normalize_key

if TYPE_CHECKING:
    from nous.brain.brain import Brain
    from nous.brain.schemas import DecisionSummary, NeighborResult
    from nous.config import Settings
    from nous.heart.facts import ExemplarHit
    from nous.heart.heart import Heart
    from nous.heart.schemas import RecallResult

logger = logging.getLogger(__name__)

# F086: memory-referential interrogatives are NOT classification-shaped.
# Deliberately narrow — trec-style classification queries ARE questions and
# must trigger. Codex r2: the ambiguous prefixes (`did i/we/you`, `what did`,
# `what have i/we`) over-matched ordinary banking77 classification shapes like
# "did I get charged twice" / "did I make a cash withdrawal", silencing the leg
# on a class the feature targets. They now require an actual stored-memory verb
# (say/tell/mention/ask/discuss/talk about) within a few words, so a bare
# past-tense question is left classification-shaped. The standalone
# memory-referential phrases (remind me, last time, ...) match on their own.
_MEMORY_VERB = r"(?:say|said|tell|told|mention(?:ed)?|ask(?:ed)?|discuss(?:ed)?|talk(?:ed)?\s+about)"
_MEMORY_REFERENTIAL = re.compile(
    r"\b(?:"
    r"(?:did\s+(?:i|we|you)|what\s+did|what\s+have\s+(?:i|we))(?:\s+\w+){0,4}?\s+"
    + _MEMORY_VERB
    + r"|remind me|last time|earlier|previous(?:ly)?"
    r"|we (?:discussed|talked)|you (?:said|told|mentioned)"
    r")\b",
    re.IGNORECASE,
)


def _is_classification_shaped(query: str, max_words: int) -> bool:
    words = query.split()
    return 0 < len(words) <= max_words and not _MEMORY_REFERENTIAL.search(query)


# Spreading-activation result window: at most this many activated nodes are
# appended per recall (the CTE's historic LIMIT). The CTE is over-fetched at
# 2x so rows dropped by content resolution (inactive/foreign/dangling —
# codex P2 round 5, PR #555) backfill from lower-ranked valid nodes instead
# of consuming the window.
_SPREADING_RESULT_CAP = 20
_SPREADING_OVERFETCH_LIMIT = 40

# Spreading-activation density gate: TTL cache per Brain instance.
# Density only moves at sleep-cycle cadence (densifier/pruning), so paying
# the graph_edges aggregate on EVERY recall with decision hits is pure
# overhead now that prod density sits above the auto-mode threshold.
# WeakKeyDictionary so a torn-down Brain frees its entry.
_DENSITY_GATE_TTL_SECONDS = 300.0
_density_gate_cache: "weakref.WeakKeyDictionary[object, tuple[float, float]]" = (
    weakref.WeakKeyDictionary()
)

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
    # F080: True iff coherent ranking was active for this call (censors +
    # procedures excluded from the ranked pool). Observability only — not
    # formatted into recall_deep text, so it does not affect the snapshot.
    coherent_ranking_applied: bool = False
    # R3.3 (F085): True iff the keyed fact leg was flag-enabled AND the query
    # yielded at least one entity candidate (regardless of whether any row
    # was actually found — mirrors chunks_searched's "eligible AND attempted"
    # semantics).
    keyed_leg_used: bool = False
    # Count of keyed PipelineResults merged at assembly time (after id-dedup
    # against every other leg, BEFORE the F071 exclude_ids filter — a keyed
    # hit dropped by exclude_ids still counts here; stats-only drift).
    n_keyed: int = 0
    # Count of keyed candidates dropped because their id already existed in
    # the result set from another leg (corroboration, not a new find).
    n_keyed_dup: int = 0
    # R3v2: count of round-2 keyed PipelineResults merged at assembly time
    # (same accounting convention as n_keyed above).
    n_keyed_r2: int = 0
    # R3v2: count of round-2 keyed candidates dropped because their id
    # already existed in the result set from another leg (same accounting
    # convention as n_keyed_dup above). codex r3: computed primarily by the
    # assembly-time filter against the FULL cross-leg existing_ids set
    # (before the K2 slice — a candidate already surfaced elsewhere must
    # never consume a K2 slot only to be dropped), plus any residual
    # dedup from _keyed_r2_to_pipeline's own redundant-belt check. codex
    # r4: also includes candidates dropped by the same pre-slice filter
    # for being F071-excluded (already in the current turn's context).
    n_keyed_r2_dup: int = 0
    # R3v2: True iff round 2 ran and EITHER the key-derivation cap or the
    # candidate-fetch cap was hit (possibly-truncated -- reaching exactly
    # the candidate LIMIT is indistinguishable from "there were more").
    keyed_r2_truncated: bool = False
    # F086: True iff the exemplar leg was flag-enabled, query was
    # classification-shaped, AND the store existence-probe returned True
    # (mirrors keyed_leg_used's "eligible AND attempted" semantics).
    exemplar_leg_used: bool = False
    # F086: count of exemplar PipelineResults merged at assembly time (after
    # id-dedup against every other leg). Same accounting convention as
    # n_keyed above.
    n_exemplar: int = 0
    # F086: count of exemplar candidates dropped because their id already
    # existed in the result set from another leg.
    n_exemplar_dup: int = 0


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

    # R3.3 Stage 1.6: keyed fact-leg rows (raw SQLAlchemy Row objects from
    # FactManager.fetch_by_entity_keys — id, content, learned_at,
    # source_ordinal, matched).
    keyed_results: list = field(default_factory=list)
    keyed_leg_used: bool = False
    # Set during assembly in run_recall_pipeline (after existing_ids from
    # every other leg is known) — not populated inside _run_stages.
    n_keyed_dup: int = 0

    # R3v2: round-2 keyed fact-leg rows (same Row shape as keyed_results),
    # FULLY RANKED by _rank_r2_candidates but NOT yet sliced to K2 (codex
    # r3: K2 selection moves to assembly time, after existing_ids is
    # complete across every leg — see run_recall_pipeline). Populated
    # inside _run_stages when keyed_fact_leg_rounds >= 2 and round 1
    # produced at least one hit.
    keyed_r2_results: list = field(default_factory=list)
    keyed_r2_truncated: bool = False
    # codex r3: True iff round 2 was attempted (mirrors chunks_searched's
    # "eligible AND attempted" semantics) — consumed at assembly time to
    # gate the combined keyed_r2 telemetry log line (re-added: this field
    # was removed as dead state by the final review's Minor 3 before the
    # telemetry line moved to assembly and needed a "did round 2 run" gate
    # again).
    keyed_r2_ran: bool = False
    # codex r3: stage-time counters threaded through to assembly so the
    # single combined telemetry log line there can report them alongside
    # the assembly-time selection count.
    keyed_r2_keys_examined: int = 0
    # codex r5: fetched candidate count. Now r1-FREE by construction —
    # fetch_by_entity_keys excludes r1 ids in the SQL itself (exclude_fact_ids),
    # so this no longer describes a "pre-r1-filter" fan-out count (it did
    # before r5); the Python-side r1_id_set filter downstream is belt-and-
    # suspenders and expected to drop 0 rows.
    keyed_r2_candidates: int = 0
    # Set during assembly (mirrors n_keyed_dup above) — not populated
    # inside _run_stages.
    n_keyed_r2_dup: int = 0

    # F086: Stage 1.7 exemplar-leg hits — ExemplarHit rows already filtered
    # by the similarity floor. Set during assembly's own n_exemplar_dup
    # accounting (mirrors keyed_results above).
    exemplar_hits: list = field(default_factory=list)
    exemplar_leg_used: bool = False

    # Flags
    searched_decisions: bool = False
    searched_heart: bool = False
    coherent_ranking_applied: bool = False  # F080: filter actually ran this call
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
    # F075 L3: parse the date window once per query when the leg is enabled.
    # The parser is fail-open (returns None on timeout/error/no-date), so
    # no try/except is needed; None → no leg → byte-identical to today.
    date_window = None
    parser = getattr(heart, "date_window_parser", None)
    # Only the fact leg consumes the window, so skip the Haiku parse (latency +
    # budget) when facts aren't in scope, e.g. memory_types=["decision"] (codex P2).
    # `not memory_types` covers None AND [] — both default to "all" in _run_stages
    # (`memory_types or ["all"]`), so facts are searched and the parse must run.
    _facts_in_scope = not memory_types or "all" in memory_types or "fact" in memory_types
    if getattr(settings, "date_leg_enabled", False) and parser is not None and _facts_in_scope:
        import datetime as _dt
        date_window = await parser.parse(query, _dt.date.today())

    acc = await _run_stages(
        query, heart, brain, settings, limit, memory_types,
        residual_activations, apply_mmr=apply_mmr, date_window=date_window,
        exclude_ids=exclude_ids,
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

    # R3.3 (F085): keyed fact leg assembly. Additive-only placement — each
    # keyed hit is inserted before the first existing result with a strictly
    # lower score. Only keyed hits move; every existing result keeps its
    # relative order — attribution-clean for the MAB flag-on/off A/B, and
    # works identically on the rerank=False (multi_turn_eval) and
    # rerank=True (retrieval_runner) paths. When rerank_by_score=True the
    # later global sort yields the same final order. A tail-append would
    # sit at position 11+ under the rerank=False default and be sliced off
    # by [:top_k] — the leg would measure as a no-op on the acceptance
    # harness. No-op (empty acc.keyed_results) when the flag is off.
    existing_ids = {r.id for r in results}
    keyed, acc.n_keyed_dup = _keyed_to_pipeline(acc.keyed_results, settings, existing_ids)
    for kr in keyed:
        pos = next(
            (i for i, r in enumerate(results) if (r.score or 0.0) < kr.score),
            len(results),
        )
        results.insert(pos, kr)

    # R3v2: round-2 keyed assembly — same additive-only, score-ordered
    # insertion pattern as round 1 above, run strictly after it so round-2
    # hits land after round-1 hits positionally (band is derived below
    # round-1's floor, so this also falls out of the score comparison).
    existing_ids.update(r.id for r in keyed)  # arch-P2-2: single source of truth
    # codex r3: K2 selection happens HERE, not in Stage 1.6 — only here is
    # existing_ids complete across every leg (heart/graph/decisions/chunks/
    # r1-keyed). acc.keyed_r2_results is the FULL ranked candidate list from
    # the stage (unsliced); filter it against existing_ids first, THEN take
    # the top K2 of what remains. Selecting K2 before this point could let a
    # candidate already surfaced elsewhere consume the only K2 slot and get
    # dropped right here — silently wasting the slot instead of giving it to
    # a fresh hop candidate ranked just below it.
    #
    # codex r4: ALSO filter against the F071 exclude_ids set (facts already
    # in the current turn's system prompt) for the exact same reason — a
    # top-ranked hop fact that happens to be F071-excluded would otherwise
    # consume the only K2 slot and die at the F071 filter below (:450-455),
    # instead of a fresh candidate. Round-1 keyed deliberately does NOT
    # pre-filter against F071 (v1's accepted convention — n_keyed is
    # documented as counting "BEFORE the F071 exclude_ids filter"); round-2
    # does, because its selection already happens here at assembly where
    # the F071 set is available, and K2 slots are scarce enough that
    # wasting one on a candidate already known to be excluded is a real
    # loss round-1's plain allotment doesn't share.
    ranked_r2 = acc.keyed_r2_results
    f071_fact_excludes = (exclude_ids or {}).get("fact", set())
    filtered_r2 = [
        r for r in ranked_r2
        if r.id not in existing_ids and str(r.id) not in f071_fact_excludes
    ]
    acc.n_keyed_r2_dup = len(ranked_r2) - len(filtered_r2)
    k2 = getattr(settings, "keyed_fact_leg_k2", 8)
    r2_survivors = filtered_r2[:k2]
    if r2_survivors:
        await heart.facts.track_access([r.id for r in r2_survivors])
    keyed_r2, extra_r2_dup = _keyed_r2_to_pipeline(r2_survivors, settings, existing_ids)
    # redundant belt (mirrors codex r1's vocab-filter pattern): r2_survivors
    # is already disjoint from existing_ids by construction above, so this
    # should always be 0 — kept for defense if that invariant ever breaks.
    acc.n_keyed_r2_dup += extra_r2_dup
    for kr in keyed_r2:
        pos = next(
            (i for i, r in enumerate(results) if (r.score or 0.0) < kr.score),
            len(results),
        )
        results.insert(pos, kr)

    # codex r3: ONE combined telemetry line at assembly, carrying both the
    # stage-time counters (threaded through the accumulator) and the
    # assembly-time selection count — "selected" can only be known here,
    # after cross-leg dedup. Fires only when round 2 was attempted (mirrors
    # chunks_searched's "eligible AND attempted" semantics).
    if acc.keyed_r2_ran:
        logger.info(
            "keyed_r2: r1_hits=%d keys_examined=%d candidates=%d selected=%d truncated=%s",
            len(acc.keyed_results), acc.keyed_r2_keys_examined,
            acc.keyed_r2_candidates, len(keyed_r2), acc.keyed_r2_truncated,
        )

    # F086: exemplar leg assembly — same additive-only, score-ordered
    # insertion pattern as the keyed leg above. existing_ids is recomputed
    # fresh here (rather than reusing the keyed block's set) since keyed_r2
    # rows were inserted into ``results`` after that set was captured.
    # No-op (empty acc.exemplar_hits) when the flag is off or nothing
    # cleared the similarity floor.
    n_exemplar_dup = 0
    exemplar_rows: list[PipelineResult] = []
    if acc.exemplar_hits:
        # Codex r10: universal replace-at-merge. The leg fired with post-floor
        # hits, so those exemplar facts must render ONLY in the dedicated
        # examples block — never doubled under Heart Memory. Any leg can surface
        # an exemplar fact UNTAGGED into `results`: Stage 1 (ordinary recall),
        # Stage 2b (graph neighbors — `extracted_from` edges exist because
        # exemplar ingest sets source_episode_id), or spreading/Stage 4. Remove
        # every such untagged row whose id is in the post-floor fetched-hit set,
        # regardless of which stage added it, then re-insert them as tagged rows
        # below. Replacement-guaranteed (r2 lesson): ONLY ids in the fetched set
        # are removed, so a fetch failure / all-below-floor leaves `results`
        # untouched, and an exemplar-source row NOT in the fetched set stays
        # wherever it came from. `existing_ids` is recomputed AFTER the removal
        # so the score-banded insertion below sees the pruned list.
        fetched_ids = {h.id for h in acc.exemplar_hits}
        results = [r for r in results if not (r.id in fetched_ids and r.metadata.get("retrieval_leg") != "exemplar")]
        existing_ids = {r.id for r in results}
        exemplar_rows, n_exemplar_dup = _exemplar_to_pipeline(acc.exemplar_hits, settings, existing_ids)
        for er in exemplar_rows:
            pos = next(
                (i for i, r in enumerate(results) if (r.score or 0.0) < er.score),
                len(results),
            )
            results.insert(pos, er)

    # Attach contradiction links to matching results
    if acc.contradictions:
        _attach_contradictions(results, acc.contradictions)

    # §1: event_date-only recency conflict resolution (same-subject conflicting
    # facts). Runs on the full cross-leg candidate set with contradiction links
    # present, BEFORE the rerank_by_score sort below.
    if getattr(settings, "recency_resolver_enabled", False):
        results = _resolve_recency_conflicts(results, settings)

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

    # F044 v1.1: record edges among the final co-retrieved set as STC recall
    # reactivations. Read-only here (one query); the ltp write is deferred to
    # the sleep flush, so the recall path stays write-free.
    if (
        getattr(settings, "tinyhippo_lite_enabled", False)
        and getattr(settings, "tinyhippo_recall_touch_enabled", False)
    ):
        await _record_recall_reactivation(brain, results)

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
        coherent_ranking_applied=acc.coherent_ranking_applied,  # F080: reflects the filter actually running (search_all only)
        keyed_leg_used=acc.keyed_leg_used,  # R3.3 (F085)
        n_keyed=len(keyed),
        n_keyed_dup=acc.n_keyed_dup,
        n_keyed_r2=len(keyed_r2),  # R3v2
        n_keyed_r2_dup=acc.n_keyed_r2_dup,  # R3v2
        keyed_r2_truncated=acc.keyed_r2_truncated,  # R3v2
        exemplar_leg_used=acc.exemplar_leg_used,  # F086
        n_exemplar=len(exemplar_rows),  # F086
        n_exemplar_dup=n_exemplar_dup,  # F086
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
    date_window: "object | None" = None,
    exclude_ids: dict[str, set[str]] | None = None,
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

        # F080: coherent ranking makes the IMPLICIT recall pool knowledge-only.
        # Censors and procedures are excluded from the default ("all") search —
        # they have dedicated surfaces and would otherwise compete on incomparable
        # score scales (raw-cosine censor floor >=0.7; procedure utility boost >1.0).
        # Gated on ``search_all`` so an EXPLICIT memory_types=["procedure"]/["censor"]
        # request is still honored (the advertised tool contract + procedure-only
        # eval probes, codex P1). recall_deep-only — the cognitive path uses per-type
        # search_*, not heart.recall.
        if search_all and getattr(settings, "coherent_ranking_enabled", False):
            heart_types = [t for t in heart_types if t not in ("censor", "procedure")]
            acc.coherent_ranking_applied = True

        if heart_types:
            acc.searched_heart = True
            acc.heart_types_searched = heart_types
            heart_results = await heart.recall(
                query, limit=limit, types=heart_types,
                residual_activations=residual_activations,  # F055
                apply_mmr=apply_mmr,  # F030.2
                date_window=date_window,  # F075 L3
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
                settings=settings,
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
    # Stage 1.6 (R3.3, F085): keyed fact leg — land-dark, flag + entity-
    # presence gated (NOT frame-gated: the eval harness has no frame concept).
    # Gated on the same search_all/"fact" condition as Stage 1.5's chunk leg
    # (codex P2): a memory_types=["episode"] request has no fact-shaped
    # consumer for these hits, so skip the vocab lookup + DB query entirely.
    # ------------------------------------------------------------------
    if getattr(settings, "keyed_fact_leg_enabled", False) and (
        search_all or "fact" in search_types
    ):
        try:
            vocab = await heart.facts.entity_key_vocabulary()
            candidates = extract_entity_candidates(query, vocab=vocab)
            if candidates:
                acc.keyed_leg_used = True
                acc.keyed_results = await heart.facts.fetch_by_entity_keys(
                    candidates, limit=getattr(settings, "keyed_fact_leg_k", 8)
                )
        except Exception as exc:
            acc.stage_errors["keyed"] = acc.stage_errors.get("keyed", 0) + 1
            logger.warning("keyed fact leg failed: %s", exc)

        # R3v2: bounded round 2 — one deterministic, zero-LLM iterative hop
        # over round-1 hits' own entity keys (Task 2, docs/superpowers/plans/
        # 2026-07-19-r3v2-iterative-keyed.md). Own try/except (amendment 10)
        # so a round-2 failure can never take round-1's already-populated
        # acc.keyed_results down with it. Gated on acc.keyed_results being
        # non-empty, which also guarantees `vocab`/`candidates` above ran to
        # completion without raising.
        rounds = getattr(settings, "keyed_fact_leg_rounds", 1)
        if rounds >= 2 and acc.keyed_results:
            try:
                acc.keyed_r2_ran = True
                r1_ids = [row.id for row in acc.keyed_results]
                key_map = await heart.facts.entity_keys_for_facts(r1_ids)
                r2_keys: list[str] = []
                seen_k = set(candidates)  # MINUS round-1 query keys
                # (1) exact + cheap: the round-1 hits' own entity rows, in
                #     r1 rank order, alphabetical within a fact
                #     (entity_keys_for_facts already sorts alphabetically).
                for rid in r1_ids:
                    for k in key_map.get(rid, []):
                        if k not in seen_k:
                            seen_k.add(k)
                            r2_keys.append(k)
                # (2) spec 2.2 primary definition: vocab keys appearing in
                #     round-1 fact CONTENTS (covers entities mentioned but
                #     not indexed on the hit itself). CRITICAL (devil-P1a,
                #     sharpened by codex r1): extract_entity_candidates'
                #     quoted + capitalized-span legs emit arbitrary
                #     NON-INDEXED spans first, and its own final
                #     out[:max_candidates] slice is span-first — so on a raw
                #     call, >= max_keys junk spans in a fact's content can
                #     exhaust the cap before the vocab leg's real match ever
                #     gets appended far enough forward to survive the slice.
                #     vocab_only=True below skips those spans entirely so
                #     only vocab members are ever collected. Only VOCAB
                #     MEMBERS may enter the round-2 key set.
                max_keys = getattr(settings, "keyed_fact_leg_r2_max_keys", 32)
                for row in acc.keyed_results:
                    if len(r2_keys) >= max_keys:
                        # codex r2: this break always skips at least one
                        # remaining r1 hit's content scan (this row and any
                        # after it in the loop) — "possibly truncated" under
                        # the same accepted convention as the candidate-cap
                        # `>=` check below: an exact fit that happens to
                        # have nothing more to find still reads as capped,
                        # since we stopped looking before finding out. Only
                        # the strict `> max_keys` check after this loop
                        # caught OVERFLOW from step 1 alone; it can't catch
                        # this early-exit, so it's flagged here instead.
                        acc.keyed_r2_truncated = True
                        break
                    # codex r6: extract UNCAPPED (max_candidates=None), then
                    # filter against seen_k FIRST, THEN cap to the remaining
                    # budget — in that order. The r1-era vocab_only fix (r1)
                    # keeps junk spans out, but the extractor's own
                    # max_candidates cap still applies to the RAW (pre-seen_k
                    # -filter) match list; a row whose content mentions
                    # >= max_keys keys that are ALREADY seen (from step 1 or
                    # an earlier row) before a fresh key can fill that cap
                    # with useless already-seen matches, truncating the
                    # fresh key away before this loop's own `k not in
                    # seen_k` filter ever gets a chance to see it. vocab_only
                    # mode is bounded by this row's own content tokens, so
                    # uncapped extraction here is cheap.
                    fresh = [
                        k for k in extract_entity_candidates(
                            row.content, vocab=vocab, max_candidates=None,
                            vocab_only=True,  # codex r1: quoted/cap-span junk
                            # would otherwise exhaust the extractor's internal
                            # cap before its own vocab leg's real match ever
                            # reaches this loop (see keys.py docstring).
                        )
                        if k in vocab and k not in seen_k  # redundant belt (codex r1): cheap, guards any future extractor change
                    ]
                    remaining = max_keys - len(r2_keys)
                    if len(fresh) > remaining:
                        # codex r6: this row alone had more fresh keys than
                        # the remaining budget — some are dropped right here,
                        # possibly with no later row/iteration left to catch
                        # it via the `>=` break above (e.g. this is the last
                        # row in acc.keyed_results). Flag it directly so the
                        # r2 exact-cap truncation semantics hold at this
                        # finer (within-row) granularity too.
                        acc.keyed_r2_truncated = True
                    for k in fresh[:remaining]:
                        seen_k.add(k)
                        r2_keys.append(k)
                if len(r2_keys) > max_keys:
                    acc.keyed_r2_truncated = True
                    r2_keys = r2_keys[:max_keys]
                acc.keyed_r2_keys_examined = len(r2_keys)
                rows2_count = 0
                if r2_keys:
                    max_cand = getattr(settings, "keyed_fact_leg_r2_max_candidates", 256)
                    # codex r5: exclude r1 ids IN THE QUERY, not just after —
                    # derived r2_keys often match the r1 facts' own entity
                    # rows (they seeded those keys), so without this, the
                    # capped LIMIT could fill with r1 rows guaranteed to be
                    # dropped by the Python-side filter below, starving a
                    # fresh hop candidate just beyond the LIMIT of ever
                    # being fetched at all.
                    #
                    # codex r6: widen the exclusion to every fact id ALREADY
                    # known dead at this point in the pipeline — Stage 1's
                    # own fact hits (acc.heart_results, type=="fact"; Stage
                    # 2+/decisions/graph-expanded haven't run yet, this is
                    # Stage 1.6) and the F071 context-exclusion set
                    # (exclude_ids["fact"]) can ALSO fill the capped LIMIT
                    # and die at assembly (round 3/4's filters there only
                    # dedup what got fetched — they can't un-cap a LIMIT
                    # that already excluded a fresh candidate). LATER-leg
                    # duplicates (Stage 2+/decisions/graph-expanded, which
                    # haven't run yet) remain the assembly filter's job —
                    # this fetch can only exclude what's knowable NOW.
                    known_dead_fact_ids = (
                        set(r1_ids)
                        | {hr.id for hr in acc.heart_results if hr.type == "fact"}
                        | {UUID(s) for s in (exclude_ids or {}).get("fact", set())}
                    )
                    rows2 = await heart.facts.fetch_by_entity_keys(
                        r2_keys, limit=max_cand, track=False,
                        exclude_fact_ids=known_dead_fact_ids,
                    )
                    rows2_count = len(rows2)
                    if len(rows2) >= max_cand:
                        acc.keyed_r2_truncated = True
                    # Belt-and-suspenders (codex r5): the SQL fetch above
                    # already excludes r1 ids, so this is expected to drop
                    # 0 rows in normal operation — kept as defense in depth
                    # if that invariant (or a future caller) ever breaks.
                    r1_id_set = set(r1_ids)
                    rows2 = [r for r in rows2 if r.id not in r1_id_set]
                    # codex r3: rank the FULL candidate list here but do NOT
                    # slice to K2 or track_access yet — K2 selection moves to
                    # assembly time (run_recall_pipeline), where the full
                    # existing_ids set across EVERY leg (heart/graph/
                    # decisions/chunks/r1-keyed) is known. Selecting K2 here
                    # let a candidate already surfaced by another leg consume
                    # the only K2 slot and get silently dropped at assembly —
                    # wasting the slot instead of giving it to a fresh hop
                    # candidate ranked just below it.
                    acc.keyed_r2_results = _rank_r2_candidates(rows2, query)
                acc.keyed_r2_candidates = rows2_count
                # Telemetry moved to assembly (run_recall_pipeline): the
                # "selected" count can only be known there, after cross-leg
                # dedup — see the combined keyed_r2 log line there.
            except Exception as exc:
                acc.stage_errors["keyed_r2"] = acc.stage_errors.get("keyed_r2", 0) + 1
                logger.warning("keyed_r2 fact leg failed: %s", exc)

    # ------------------------------------------------------------------
    # Stage 1.7 (F086): exemplar leg - K nearest labeled examples by cosine.
    # Land-dark, flag + classification-shaped-query gated. Gated on the same
    # search_all/"fact" condition as Stage 1.5/1.6 (a memory_types=["episode"]
    # request has no fact-shaped consumer for these hits).
    # ------------------------------------------------------------------
    if (
        getattr(settings, "exemplar_mode_enabled", False)
        and (search_all or "fact" in search_types)
        and _is_classification_shaped(query, getattr(settings, "exemplar_max_query_words", 64))
    ):
        try:
            if await heart.facts.has_exemplars():
                acc.exemplar_leg_used = True
                # arch-review M2: exact chunk-leg idiom (retrieval_pipeline.py
                # Stage 1.5's _search_episode_chunks); the process LRU
                # (NOUS_EMBEDDING_CACHE_SIZE) makes the repeat embed free.
                embedder = getattr(heart, "_embeddings", None)
                q_vec = (await embedder.embed(query)) if embedder is not None else None
                if q_vec:
                    # Codex r8: exclude the F071 already-in-context fact set from
                    # the fetch so ids the assembly-time F071 filter will drop do
                    # NOT spend the K budget (which would starve fresh
                    # below-LIMIT examples). This is the F071 cross-context set
                    # ONLY — Stage-1-surfaced exemplar ids are deliberately NOT
                    # excluded here (they are re-fetched by the strip-and-refetch
                    # design and land in the examples block).
                    f071_excludes = {UUID(s) for s in (exclude_ids or {}).get("fact", set())}
                    hits = await heart.facts.fetch_exemplars_by_vector(
                        q_vec,
                        limit=getattr(settings, "exemplar_top_k", 25),
                        exclude_fact_ids=f071_excludes or None,
                    )
                    floor = getattr(settings, "exemplar_min_similarity", 0.30)
                    surviving = [h for h in hits if h.similarity >= floor]
                    acc.exemplar_hits = surviving
                    if surviving:
                        # Codex r3: retrieval == access. Track recall on the
                        # post-floor survivors (the set that will merge) so
                        # stale_scan does not deactivate an actively-used exemplar
                        # once past stale_scan_age_days. Below-floor hits are
                        # never tracked. Mirrors the keyed_r2 survivors-only,
                        # sync-await precedent (assembly's track_access below).
                        #
                        # Codex r10: the Stage-1-specific strip that used to live
                        # here moved to a UNIVERSAL replace-at-merge at assembly
                        # time (see the exemplar-leg assembly block in
                        # run_recall_pipeline) — an exemplar fact can enter
                        # `results` UNTAGGED from ANY leg (Stage 1, Stage 2b graph
                        # neighbors, spreading/Stage 4), not just Stage 1, so the
                        # de-dup-and-retag has to run once over the fully assembled
                        # `results` list, not per-leg here.
                        await heart.facts.track_access([h.id for h in surviving])
        except Exception:
            logger.warning("exemplar leg failed", exc_info=True)
            acc.stage_errors["exemplar"] = acc.stage_errors.get("exemplar", 0) + 1

    # ------------------------------------------------------------------
    # Stage 2: F022 Phase 2 cross-type graph expansion from Heart seeds
    # ------------------------------------------------------------------
    if (
        heart_types
        # Fire when EITHER fact/episode results OR chunk results are present.
        # Previously required acc.heart_results, which silently blocked Path A
        # (Stage 2b) chunk-seed expansion on chunk-only retrieval (no fact/episode
        # hit) — the line-403 gap. Stage 2's decision loop no-ops on empty
        # heart_results; Stage 2b stays gated on heart_graph_all_types_enabled.
        and (acc.heart_results or acc.chunk_results)
        and settings.graph_recall_enabled
        and settings.cross_type_linking_enabled
    ):
        seen_graph: dict[UUID, "NeighborResult"] = {}
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
                        if n.node_type != "decision":
                            continue
                        prev = seen_graph.get(n.id)
                        if prev is n:
                            # Aliasing guard: the same object reached again
                            # (shared instances from mocks) must not have
                            # its stored seed_score overwritten below.
                            continue
                        # Plan 1.2: thread the seed's retrieval score
                        # (nullable — None stays None so the scorer's
                        # legacy fallback fires; never coerce to 0.0).
                        n.seed_score = hr.score
                        if prev is not None:
                            # Best-composed-path replacement (same rule as
                            # Stage 2b :578-596) is part of the seed-score
                            # mechanism — flag-gated so flag-off dedup
                            # stays first-seed-wins, byte-identical to the
                            # pre-plan-1.2 behavior.
                            if (
                                getattr(settings, "graph_neighbor_seed_score_enabled", False)
                                and _score_memory_neighbor(n, settings)
                                > _score_memory_neighbor(prev, settings)
                            ):
                                prev.seed_score = n.seed_score
                                prev.edge_weight = n.edge_weight
                                prev.edge_relation = n.edge_relation
                                prev.extraction_method = n.extraction_method
                            continue
                        acc.heart_graph_decisions.append(n)
                        seen_graph[n.id] = n
                except Exception:
                    # 3a: log (was silent) so a graph-expansion outage is
                    # diagnosable, not just counted. Still non-fatal.
                    logger.warning("Stage 2 heart-graph neighbor expansion failed", exc_info=True)
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
            seen_mem: dict[UUID, "NeighborResult"] = {}
            heart_ids: set[UUID] = {hr.id for hr in acc.heart_results}
            # acc.chunk_results carries (id, content, score, episode_id) per
            # _search_episode_chunks at line 825. Use star-unpack so future
            # tuple shape changes don't crash this hot loop.
            chunk_ids: set[UUID] = {item[0] for item in acc.chunk_results}
            # Heart seeds: top-K fact/episode results.
            # (id, node_type, seed_score) — seed_score threads the seed's own
            # retrieval score to its neighbors for the seed-score scoring fix.
            # Keep it NULLABLE: a None score must stay None so the consumer's
            # `seed_score is not None` guard routes it to the legacy fallback
            # (NOT coerce to 0.0, which would pass the guard and score the
            # neighbor 0.0 — silently sinking it).
            mem_seeds: list[tuple[UUID, str, float | None]] = [
                (hr.id, hr.type, hr.score) for hr in acc.heart_results[:3]
                if hr.type in ("fact", "episode")
            ]
            # Chunk seeds: top-K F067 chunk-recall results (when present).
            # Chunks have rich same-episode neighborhoods via F070.
            # chunk_results items are (id, content, score, episode_id).
            mem_seeds.extend(
                (item[0], "chunk",
                 float(item[2]) if len(item) > 2 and item[2] is not None else None)
                for item in acc.chunk_results[:3]
            )
            # Per-type fan-out — one ``LIMIT`` window per neighbor type so
            # chunks don't crowd facts/episodes (or vice versa) out of a
            # single small union limit. Order is irrelevant; the global
            # rerank handles final ordering.
            mem_neighbor_types = ("fact", "episode", "chunk", "procedure")
            for seed_id, seed_type, seed_score in mem_seeds:
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
                        if n.id in seen_mem:
                            # Reached from multiple seeds: keep the PATH (seed + edge)
                            # with the highest COMPOSED score, not just the strongest
                            # seed. First-seed-wins could permanently under-score a
                            # neighbor a later, higher-scored seed also reaches. But
                            # updating seed_score alone while keeping the first path's
                            # edge_weight/extraction_method would score a path that
                            # does not exist (a later strong seed through a weak edge
                            # combined with the first path's strong edge). Compare the
                            # full composed score and replace the stored neighbor's
                            # path metadata when the later path genuinely wins.
                            prev = seen_mem[n.id]
                            n.seed_score = seed_score
                            if _score_memory_neighbor(n, settings) > _score_memory_neighbor(prev, settings):
                                prev.seed_score = n.seed_score
                                prev.edge_weight = n.edge_weight
                                prev.edge_relation = n.edge_relation
                                prev.extraction_method = n.extraction_method
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
                        # Carry the seed's retrieval score for the seed-score fix.
                        n.seed_score = seed_score
                        acc.heart_graph_memory_neighbors.append(n)
                        seen_mem[n.id] = n

    # ------------------------------------------------------------------
    # Stage 3+4: Brain decisions + graph expansion
    # ------------------------------------------------------------------
    decision_results: list["DecisionSummary"] = []
    graph_expanded: list["NeighborResult"] = []

    if search_all or "decision" in search_types:
        acc.searched_decisions = True
        decision_results = await brain.query(query, limit=limit)

    # F022 extension (2026-07-11): heart FACT seeds for spreading. Top-3
    # fact results with their RRF scores — same seed shape as decision
    # seeds (coherent normalizer) and same cap as Path A's heart seeds.
    # Spreading fires on decision-less corpora and leverages the
    # fact/chunk graph instead of decisions only. Default behavior per
    # owner directive (MAB paired A/B: 0 memory regressions). Deliberately
    # OUTSIDE the decision-search gate (codex P2 round 4, PR #556) so
    # Heart-only scopes (memory_types=["fact"]) can seed spreading too.
    heart_fact_seeds: list[tuple[UUID, str, float]] = [
        (hr.id, "fact", float(hr.score))
        for hr in acc.heart_results
        if hr.type == "fact" and hr.score is not None
    ][:3]

    # F022: graph expansion — expand top decisions (heart seeds also
    # fire spreading even with zero decision hits / no decision scope).
    if settings.graph_recall_enabled and (decision_results or heart_fact_seeds):
        seen_ids: set[UUID] = {d.id for d in decision_results}

        # F022 Phase 4: density-gated spreading activation
        use_spreading = False
        mode = str(settings.spreading_activation_enabled).lower()
        if mode == "true":
            # Forced on — no need to pay the density aggregate query.
            use_spreading = True
        elif mode != "false":
            # auto — density-gated, TTL-cached per Brain instance.
            try:
                from nous.brain.spreading_activation import (
                    compute_graph_density,
                    should_use_spreading_activation,
                )

                now_ts = time.monotonic()
                cached = _density_gate_cache.get(brain)
                if (
                    cached is not None
                    and now_ts - cached[1] < _DENSITY_GATE_TTL_SECONDS
                ):
                    density = cached[0]
                else:
                    async with brain.db.session() as sa_session:
                        density = await compute_graph_density(
                            sa_session, brain.agent_id
                        )
                    _density_gate_cache[brain] = (density, now_ts)
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
                    ] + heart_fact_seeds
                    # Heart-seeded spreading re-reaches existing heart/
                    # chunk candidates AND prior graph-stage outputs
                    # (Stage 2 decisions, Path A neighbors) constantly —
                    # exclude all of them so the same item never ranks
                    # twice (codex P2, PR #556: in the decision-less
                    # path seen_ids starts empty, so graph-stage ids
                    # must be excluded here too).
                    candidate_ids: set[UUID] = (
                        {hr.id for hr in acc.heart_results}
                        | {item[0] for item in acc.chunk_results}
                        | {n.id for n in acc.heart_graph_decisions}
                        | {n.id for n in acc.heart_graph_memory_neighbors}
                    )
                    seed_ids = {s[0] for s in seeds}
                    # Known duplicates are excluded INSIDE the CTE's
                    # final SELECT (before its LIMIT) so they never
                    # consume the result window (codex P2 round 2,
                    # PR #556). The python-side guards below stay as a
                    # belt for callers/mocks that ignore exclude_ids.
                    activated = await spreading_activation_search(
                        sa_session, brain.agent_id, seeds, settings,
                        limit=_SPREADING_OVERFETCH_LIMIT,
                        exclude_ids=seed_ids | seen_ids | candidate_ids,
                    )
                    hits = [
                        (nid, ntype, activation)
                        for nid, ntype, activation in activated
                        if nid not in seed_ids
                        and nid not in seen_ids
                        and nid not in candidate_ids
                        and activation > 0.1
                    ]
                    # Resolve real content + created_at (shared with
                    # brain._neighbors). Ids the resolver omits —
                    # inactive facts/procedures, missing rows — are
                    # DROPPED rather than surfaced as "[<ntype>] <uuid>"
                    # placeholders that ship no information to the LLM
                    # yet consume ranking slots.
                    ids_by_type: dict[str, list[UUID]] = {}
                    for nid, ntype, _activation in hits:
                        ids_by_type.setdefault(ntype, []).append(nid)
                    descriptions = (
                        await brain._resolve_node_descriptions(
                            sa_session, ids_by_type
                        )
                        if ids_by_type
                        else {}
                    )
                    n_appended = 0
                    for nid, ntype, activation in hits:
                        if n_appended >= _SPREADING_RESULT_CAP:
                            break
                        resolved = descriptions.get(nid)
                        if resolved is None or not resolved[0]:
                            continue
                        desc, created = resolved
                        graph_expanded.append(
                            NeighborResult(
                                id=nid,
                                node_type=ntype,
                                description=desc,
                                edge_relation="spreading_activation",
                                edge_weight=activation,
                                created_at=created or datetime.now(UTC),
                            )
                        )
                        seen_ids.add(nid)
                        n_appended += 1
                if n_appended > 0:
                    acc.spreading_activation_used = True
                else:
                    # Every activated node was dropped by resolution
                    # (inactive/foreign/dangling). Fall back to 1-hop
                    # rather than shipping an empty graph expansion.
                    logger.debug(
                        "Spreading resolved 0 of %d hits; using 1-hop",
                        len(hits),
                    )
                    use_spreading = False
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
            seen_hop: dict[UUID, "NeighborResult"] = {}
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
                        prev = seen_hop.get(n.id)
                        if prev is n:
                            # Aliasing guard — see Stage 2.
                            continue
                        # Plan 1.2: thread the expanding decision's score
                        # (guaranteed non-None here by the guard above).
                        n.seed_score = dec.score
                        if n.id in seen_ids:
                            # prev is None when the id is a seed decision or
                            # a spreading leftover — skip without compare.
                            if (
                                prev is not None
                                and getattr(settings, "graph_neighbor_seed_score_enabled", False)
                                and _score_memory_neighbor(n, settings)
                                > _score_memory_neighbor(prev, settings)
                            ):
                                prev.seed_score = n.seed_score
                                prev.edge_weight = n.edge_weight
                                prev.edge_relation = n.edge_relation
                                prev.extraction_method = n.extraction_method
                            continue
                        graph_expanded.append(n)
                        seen_hop[n.id] = n
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
            # 3a: log (was silent) so a contradiction-query outage is
            # diagnosable, not just counted. Still non-fatal.
            logger.warning("Stage 5 contradiction detection failed", exc_info=True)
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
                "SELECT source_id::text, target_id::text, weight, consolidation_state "
                "FROM brain.graph_edges "
                "WHERE agent_id = :a "
                "  AND relation != 'contradicts' "
                "  AND source_id = ANY(CAST(:ids AS uuid[])) "
                "  AND target_id = ANY(CAST(:ids AS uuid[]))"
            ), {"a": brain.agent_id, "ids": candidate_ids})).all()
    except Exception:
        # R-11: fail-open (boost is a refinement) but log — previously silent,
        # so a broken graph table degraded ranking with no operator signal.
        logger.warning("Adjacency boost failed; returning unboosted results", exc_info=True)
        return results

    # F044 v1.1: weight consolidated edges higher so STC consolidation actually
    # influences ranking — a candidate connected via a frequently-reactivated
    # (consolidated) edge gets a larger adjacency degree than one connected via a
    # provisional (tagged) edge. Inert unless tinyhippo_lite_enabled.
    # Gate on a dedicated opt-in flag, NOT the master flag: flipping
    # tinyhippo_lite_enabled alone (shadow/telemetry mode) must not change
    # ranking even when graph_adjacency_boost_enabled is on, or it contaminates
    # the A/B baseline. The boost is an active mechanism, sibling to downscale.
    s_cfg = getattr(brain, "settings", None)
    stc_on = bool(getattr(s_cfg, "tinyhippo_lite_enabled", False)) and bool(
        getattr(s_cfg, "tinyhippo_consolidated_boost_enabled", False)
    )
    cons_factor = float(getattr(s_cfg, "tinyhippo_consolidated_boost_factor", 1.0)) if stc_on else 1.0

    # degree = consolidation-scaled adjacency; raw_degree = unscaled baseline.
    # We normalize by the RAW max, not the scaled max — otherwise the ×factor
    # scales both numerator and denominator and cancels out (capping a
    # consolidated candidate's boost at the tagged ceiling instead of pushing it
    # above). With raw-max normalization a consolidated-connected candidate's
    # ratio exceeds 1.0 and the factor actually bites. When stc is off,
    # degree == raw_degree, so OFF behavior is byte-identical to pre-F044.
    degree: dict[str, float] = {}
    raw_degree: dict[str, float] = {}
    for row in rows:
        src, tgt, w = row[0], row[1], float(row[2] or 0.0)
        eff = w * cons_factor if (stc_on and len(row) > 3 and row[3] == "consolidated") else w
        degree[src] = degree.get(src, 0.0) + eff
        degree[tgt] = degree.get(tgt, 0.0) + eff
        raw_degree[src] = raw_degree.get(src, 0.0) + w
        raw_degree[tgt] = raw_degree.get(tgt, 0.0) + w

    if not degree:
        return results
    max_deg = max(raw_degree.values())
    if max_deg <= 0:
        return results

    boosted = []
    for r in results:
        d = degree.get(str(r.id), 0.0)
        boost = 1.0 + alpha * (d / max_deg)
        boosted.append(replace(r, score=(r.score or 0.0) * boost))
    boosted.sort(key=lambda r: r.score or 0.0, reverse=True)
    return boosted


async def _record_recall_reactivation(
    brain: "Brain", results: list[PipelineResult],
) -> None:
    """F044 v1.1: buffer edges among co-retrieved results as STC reactivations.

    An edge whose BOTH endpoints appear in this recall's final result set was
    reactivated by the retrieval (retrieval == reactivation). We read those
    edges (excluding ``contradicts``, mirroring the adjacency boost) and buffer
    their (source, target, relation) keys in-process; the ltp write happens at
    the next sleep flush. Fails open — reinforcement is best-effort.

    Deterministic/structural edges (``extraction_method = 'deterministic'`` —
    F070 chunk ``part_of`` anchors etc.) are excluded: the write-side LTP hook
    and the downscale both skip them, so reinforcing them here would inflate
    consolidation telemetry and hand structural anchors the consolidated boost.
    This keeps all three F044 reinforcement touchpoints on the same eligible set.
    """
    from sqlalchemy import text as sa_text

    if not results or len(results) < 2:
        return
    candidate_ids = [str(r.id) for r in results]
    try:
        async with brain.db.session() as s:
            rows = (await s.execute(sa_text(
                "SELECT source_id::text, target_id::text, relation "
                "FROM brain.graph_edges "
                "WHERE agent_id = :a "
                "  AND relation != 'contradicts' "
                "  AND extraction_method IS DISTINCT FROM 'deterministic' "
                "  AND source_id = ANY(CAST(:ids AS uuid[])) "
                "  AND target_id = ANY(CAST(:ids AS uuid[]))"
            ), {"a": brain.agent_id, "ids": candidate_ids})).all()
    except Exception:
        logger.warning("F044 recall reactivation read failed; skipping", exc_info=True)
        return
    if rows:
        from nous.brain.tinyhippo_lite import record_recall_touches
        record_recall_touches(
            [(src, tgt, rel) for src, tgt, rel in rows], brain.agent_id
        )


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


def _keyed_to_pipeline(
    rows, settings: "Settings", existing_ids: set,
) -> tuple[list[PipelineResult], int]:
    """R3.3 (F085): convert keyed fact-leg rows into PipelineResults.

    Additive-only: fresh PipelineResults, id-deduped against every other
    leg, scores in a bounded band UNDER the RRF head (base - 0.005*rank,
    clamped >= 0) so keyed hits can enter context without displacing
    higher-scoring direct/chunk hits (the -5.0pp lesson).

    codex P2: metadata also carries ``subject``/``event_date`` in the exact
    convention ``_heart_results_to_pipeline`` uses (heart.py:1205-1219) —
    ``subject`` forwarded raw (``str | None``), ``event_date`` as an
    isoformat string with the key omitted entirely when absent — so
    ``_resolve_recency_conflicts`` groups keyed-only dated facts the same as
    facts surfaced via Stage 1.

    codex P2 round 6: metadata also carries ``source_episode_id`` (string,
    key omitted when absent) so the formatter's session-grouping can bucket
    keyed hits under their real episode instead of "-- Other --".
    ``_attach_fact_source_episodes`` runs BEFORE this leg's results are
    merged into ``run_recall_pipeline``'s output list (stage-order fact, not
    a bug to reorder around), so it can never attach this field to a keyed
    hit — the data has to arrive already-populated from
    ``fetch_by_entity_keys``'s own SELECT.
    """
    out: list[PipelineResult] = []
    dups = 0
    base = getattr(settings, "keyed_fact_leg_score", 0.55)
    for rank, row in enumerate(rows):
        if row.id in existing_ids:
            dups += 1
            continue
        metadata = {
            "retrieval_leg": "keyed",
            "matched_keys": int(row.matched),
            "subject": row.subject,
        }
        if row.event_date is not None:
            metadata["event_date"] = row.event_date.isoformat()
        if row.source_episode_id:
            metadata["source_episode_id"] = row.source_episode_id
        out.append(PipelineResult(
            id=row.id, type="fact",
            description=row.content, score=max(0.0, base - 0.005 * rank),
            source="heart",
            metadata=metadata,
        ))
    return out, dups


def _exemplar_to_pipeline(
    hits: "list[ExemplarHit]", settings: "Settings", existing_ids: set,
) -> tuple[list[PipelineResult], int]:
    """F086: convert exemplar-leg hits into PipelineResults.

    Mirrors ``_keyed_to_pipeline``: additive-only, id-deduped against every
    other leg, scores in a bounded band (base - 0.005*rank, clamped >= 0) so
    exemplar hits can enter context without displacing higher-scoring
    direct/chunk hits (the -5.0pp lesson).
    """
    out: list[PipelineResult] = []
    dups = 0
    base = getattr(settings, "exemplar_leg_score", 0.55)
    for rank, hit in enumerate(hits):
        if hit.id in existing_ids:
            dups += 1
            continue
        out.append(PipelineResult(
            id=hit.id, type="fact",
            description=hit.content, score=max(0.0, base - 0.005 * rank),
            source="heart",
            metadata={
                "retrieval_leg": "exemplar",
                "label": parse_label(hit.content),
                "similarity": hit.similarity,
            },
        ))
    return out, dups


def _fold_tokens(s: "str | None") -> set:
    """R3v2: normalize + tokenize a field for word-overlap ranking. Shared by
    both the query and candidate fields so both sides fold identically."""
    return set((normalize_key(s or "", max_len=1000) or "").split())


def _rank_r2_candidates(rows: list, query: str) -> list:
    """R3v2: THE SIMULATED ROUND-2 RANKING POLICY.

    Sim-parity contract (plan Global Constraints, amendment 2): any change
    to this sort key requires MAB re-simulation before the gate-3 replay —
    this is not an implementation detail free to tune.

    Sort key: attribute-key word overlap with the query (desc) -> content
    word overlap (desc) -> recency (desc) -> id (asc, final tie-break for
    full determinism — the one documented liberty beyond the spec's three
    stated criteria).

    codex r3: returns the FULL sorted list — no K2 slice here. K2 selection
    moved to assembly time (run_recall_pipeline), where the complete
    existing_ids set across every leg is known; ranking (this function) and
    selection (how many survive, and which) are now separate concerns.
    """
    qt = _fold_tokens(query)

    def _sort_key(row):
        attr = len(qt & _fold_tokens(row.attribute_key))
        content = len(qt & _fold_tokens(row.content))
        return (-attr, -content, -row.learned_at.timestamp(), str(row.id))

    return sorted(rows, key=_sort_key)


def _keyed_r2_to_pipeline(
    rows: list, settings: "Settings", existing_ids: set,
) -> tuple[list[PipelineResult], int]:
    """R3v2: convert round-2 keyed candidates into PipelineResults.

    Clone of ``_keyed_to_pipeline`` (same matched/subject/event_date/
    source_episode_id metadata conventions) with ``retrieval_leg="keyed_r2"``
    and a band derived TWO decay steps under round-1's worst rank
    (``keyed_fact_leg_k + 1``, not ``k - 1``) — a deliberate safety margin
    (devil-P3-3) so round-2 can never outscore round-1 at any configured K;
    a fixed offset would break if K were raised. Clamped >= 0; an operator
    setting ``keyed_fact_leg_score`` near 0 has effectively disabled the leg
    already, so the degenerate clamp is an accepted edge.

    codex r3: ``rows`` is expected to already be the K2 survivors selected
    by the caller (assembly time, after filtering the full ranked candidate
    list against the complete cross-leg ``existing_ids``) — the ``dups``
    counter here is a redundant belt, not the primary dedup mechanism.
    """
    out: list[PipelineResult] = []
    dups = 0
    k = getattr(settings, "keyed_fact_leg_k", 8)
    base = max(0.0, getattr(settings, "keyed_fact_leg_score", 0.55) - 0.005 * (k + 1))
    for rank, row in enumerate(rows):
        if row.id in existing_ids:
            dups += 1
            continue
        metadata = {
            "retrieval_leg": "keyed_r2",
            "matched_keys": int(row.matched),
            "subject": row.subject,
        }
        if row.event_date is not None:
            metadata["event_date"] = row.event_date.isoformat()
        if row.source_episode_id:
            metadata["source_episode_id"] = row.source_episode_id
        out.append(PipelineResult(
            id=row.id, type="fact",
            description=row.content, score=max(0.0, base - 0.005 * rank),
            source="heart",
            metadata=metadata,
        ))
    return out, dups


async def _search_episode_chunks(
    heart: "Heart",
    query: str,
    agent_id: str,
    limit: int,
    settings: "Settings | None" = None,
) -> list[tuple[UUID, str, float]]:
    """F067: search over heart.episode_chunks.

    Default (``chunk_hybrid_search_enabled`` off): vector-only cosine search,
    byte-identical to the original F067 leg; returns ``[]`` when no embedder
    is wired or the query embeds to an empty vector.

    R2 (flag on): RRF-fused vector + FTS legs via the shared
    ``heart.search.hybrid_search`` helper (the FTS leg consumes the
    ``search_tsv`` GIN index migration 050 provisioned for exactly this).
    Scores are then on the 1/k-normalized RRF [0,1] scale the coherent heart
    legs emit — not raw cosine (F080 deviant-leg renorm). With no embedder,
    the hybrid path degrades to keyword-only instead of ``[]``.

    Returns ``[(id, content, score, episode_id)]`` ordered by descending
    score. All failure modes other than the missing-embedder cases — embed
    timeout, DB error, pgvector cast failure — RAISE so the caller's
    try/except surfaces them via ``acc.stage_errors`` AND a WARN log. Silent
    fall-through to ``[]`` on embedder failure would masquerade as "no
    matches" and hide real outages.
    """
    from sqlalchemy import text as sa_text
    embedder = getattr(heart, "_embeddings", None)

    if getattr(settings, "chunk_hybrid_search_enabled", False):
        from nous.heart import search as heart_search

        query_vec = (await embedder.embed(query)) if embedder is not None else None
        query_vec = query_vec or None  # empty embed → keyword-only fallback
        async with heart.db.session() as s:
            ranked = await heart_search.hybrid_search(
                s,
                "heart.episode_chunks",
                query_vec,
                query,
                agent_id,
                limit=limit,
                active_filter=False,  # chunks have no active column
            )
            if not ranked:
                return []
            ids = [cid for cid, _ in ranked]
            rows = (await s.execute(sa_text(
                "SELECT id, content, episode_id FROM heart.episode_chunks "
                "WHERE id = ANY(:ids)"
            ), {"ids": ids})).all()
        by_id = {r[0]: (r[1], r[2]) for r in rows}
        return [
            (cid, by_id[cid][0], float(score), by_id[cid][1])
            for cid, score in ranked
            if cid in by_id
        ]

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
    return [
        PipelineResult(
            id=n.id,
            type="decision",  # Only decision neighbors are appended at this stage
            description=n.description,
            # Plan 1.2: shared Path-A scorer — seed×edge×penalty when the
            # seed-score flag is on and Stage 2 threaded seed_score; exact
            # legacy edge×decay×penalty otherwise.
            score=_score_memory_neighbor(n, settings),
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


def _score_memory_neighbor(n: "NeighborResult", settings: "Settings") -> float:
    """Composed Path-A neighbor score.

    Shared by Stage 2b's duplicate path-selection and the final scoring below, so the
    two never diverge. Seed-score fix (eval-gated): a neighbor inherits its seed's
    retrieval score discounted by edge confidence, putting it on the candidate's scale
    so a strong-seed+strong-edge neighbor can clear the top-k cutline that
    edge_weight*decay (ceiling ~0.70) structurally cannot. Falls back to the legacy
    formula when the flag is off or the seed_score wasn't threaded.
    """
    if getattr(settings, "graph_neighbor_seed_score_enabled", False) and n.seed_score is not None:
        penalty = (
            settings.graph_inferred_edge_penalty
            if (n.extraction_method or "heuristic") == "inferred"
            else 1.0
        )
        # Clamp the edge term at 1.0 — graph_edges.weight has no DB CHECK,
        # so a >1 edge would push a seed-scored neighbor above the
        # direct-hit scale, reintroducing the inflation plan 1.2 removes
        # (codex PR #558 P2; mirrors the spreading-CTE clamp).
        return n.seed_score * min(n.edge_weight, 1.0) * penalty
    return _f065_provenance_penalty(n, n.edge_weight, settings.graph_recall_decay, settings)


def _heart_graph_memory_to_pipeline(
    memory_neighbors: list["NeighborResult"], settings: "Settings"
) -> list[PipelineResult]:
    """Path A: non-decision graph-neighbor results.

    Carries the neighbor's actual ``node_type`` (fact/episode/chunk/procedure)
    so the formatter and downstream scorers can route them like any other
    memory candidate. Scoring uses the same decay + F065 provenance penalty
    pattern as the decision-neighbor path.
    """
    return [
        PipelineResult(
            id=n.id,
            type=n.node_type,
            description=n.description,
            score=_score_memory_neighbor(n, settings),
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
                # F079 P1: surface the abstract pattern so recall_deep delivers it
                # (it's on DecisionSummary but was previously dropped by the formatter).
                "pattern": d.pattern,
            },
        )
        for d in decisions
    ]


def _graph_expanded_to_pipeline(
    graph_expanded: list["NeighborResult"], settings: "Settings"
) -> list[PipelineResult]:
    return [
        PipelineResult(
            id=n.id,
            type=n.node_type,  # type: ignore[arg-type]
            description=n.description,
            # Plan 1.2: shared Path-A scorer. 1-hop rows (Stage 4 threads
            # seed_score=dec.score) score seed×edge×penalty under the flag;
            # spreading rows keep seed_score=None by design (their activation
            # already composes the seed per hop, bounded by the MAX
            # aggregation) and fall through to the legacy activation×decay.
            score=_score_memory_neighbor(n, settings),
            source=(
                "spreading_activation"
                if n.edge_relation == "spreading_activation"
                else "graph_expanded"
            ),
            edge_relation=n.edge_relation,
            # Stage origin tag — companion to _heart_graph_to_pipeline.
            # DECISION expansion results render under "Brain Decisions".
            # Non-decision nodes (facts/episodes/chunks/procedures reached
            # by spreading — common with heart fact seeds) route to the
            # typed Heart Memory section like Path A neighbors, instead of
            # being mislabeled as decisions (codex P2 round 3, PR #556).
            # Keeping this metadata in sync with the formatter's bucketing
            # logic is what makes the output stable under
            # ``rerank_by_score``.
            metadata={
                "stage_origin": (
                    "brain_graph"
                    if n.node_type == "decision"
                    else "heart_graph_memory"
                )
            },
        )
        for n in graph_expanded
    ]


def _recency_key(meta: dict) -> tuple[date | None, str]:
    """Return ``(comparable_date_or_None, "YYYY-MM" label)``. EVENT_DATE ONLY.

    ``event_date`` reaches metadata as a date-only ``"YYYY-MM-DD"`` string
    (``heart.py`` calls ``.isoformat()`` on a ``date``). Parse with
    ``date.fromisoformat`` wrapped in try/except so a malformed/None/non-str
    value FAILS OPEN to ``(None, "")`` — a no-op for that fact, NOT a crash.
    Without this, one bad event_date would take down every recall_deep call
    while the flag is on.

    Returns ``(None, "")`` when event_date is absent/None/unparseable. The
    caller treats a None date as unresolvable => no-op: a pair conflicts ONLY
    when BOTH keys are non-None AND differ. Label is month-granular for
    display. NO created_at, NO list-index fallback.
    """
    raw = meta.get("event_date")
    try:
        parsed = date.fromisoformat(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError, AttributeError):
        return (None, "")
    return (parsed, parsed.strftime("%Y-%m"))


def _resolve_recency_conflicts(
    results: list[PipelineResult],
    settings: "Settings",
    *,
    stale_penalty: float = 0.3,  # mirrors apply_supersession_filter penalty
) -> list[PipelineResult]:
    """Annotate + down-rank same-subject conflicting facts by event_date.

    Scope: ``type == "fact"`` only (chunks/episodes/decisions have no
    subject+event_date). EVENT_DATE ONLY — ordering AND the
    ``[current]``/``[superseded]`` tag use event_date. A pair contributes to
    the status map ONLY when BOTH facts have a non-None, DIFFERING event_date;
    else no-op. Frozen ``PipelineResult`` => rebuild via ``dataclasses.replace``.
    Sets ``metadata["recency_status"] = "current"|"superseded"`` and
    ``metadata["recency_date"] = "YYYY-MM"``. Down-ranks superseded by
    ``*stale_penalty`` (NOT deleted). The inline annotation is the only live
    ORDERING signal in default prod + BEAM (the down-rank re-sorts only when
    ``rerank_by_score=True``, which is False in recall_deep's default config
    AND in BEAM). The deflated score IS still printed by the formatter
    (cosmetic; nothing downstream re-reads it).
    """
    floor = float(getattr(settings, "recency_resolver_similarity_floor", 0.55))

    # Group facts by normalized non-empty subject. subject is a real
    # ``str | None`` (heart.py forwards None verbatim), so ``(x or "")`` is
    # REQUIRED — ``meta.get("subject", "")`` returns the present None and
    # None.strip() raises. Skip empty-subject facts before grouping.
    groups: dict[str, list[PipelineResult]] = {}
    for r in results:
        if r.type != "fact":
            continue
        subj = (r.metadata.get("subject") or "").strip().lower()
        if not subj:
            continue
        groups.setdefault(subj, []).append(r)

    # status_map: id -> (status, "YYYY-MM"). "superseded" wins on conflict.
    status_map: dict[UUID, tuple[str, str]] = {}

    def _mark(rid: UUID, status: str, month: str) -> None:
        if status_map.get(rid, ("",))[0] == "superseded":
            return  # already superseded by some newer value — sticky
        status_map[rid] = (status, month)

    for members in groups.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                # Gate 2: not identical (never trigger on restatement).
                if a.description.strip() == b.description.strip():
                    continue
                # Gate 3: conflict signal — strong (contradicts edge) OR
                # cheap difflib overlap fallback.
                strong = (b.id in a.contradicts) or (a.id in b.contradicts)
                if not strong:
                    ratio = difflib.SequenceMatcher(
                        None, a.description, b.description
                    ).ratio()
                    if ratio < floor:
                        continue
                # Gate 4: both event_dates present AND differing.
                ka, _ = _recency_key(a.metadata)
                kb, _ = _recency_key(b.metadata)
                if ka is None or kb is None or ka == kb:
                    continue
                # Resolve: later date => current, earlier => superseded.
                if ka > kb:
                    newer, older = a, b
                else:
                    newer, older = b, a
                _mark(newer.id, "current", newer.metadata.get("recency_date", ""))
                _mark(older.id, "superseded", "")

    if not status_map:
        return results

    # Recompute the YYYY-MM label per id from its own event_date (the _mark
    # above stored "" for the month; fill it here from the fact's own date so
    # the label always matches the fact it annotates).
    rebuilt: list[PipelineResult] = []
    for r in results:
        entry = status_map.get(r.id)
        if entry is None:
            rebuilt.append(r)
            continue
        status, _ = entry
        _, month = _recency_key(r.metadata)
        new_meta = {**r.metadata, "recency_status": status, "recency_date": month}
        if status == "superseded":
            rebuilt.append(
                replace(r, metadata=new_meta, score=(r.score or 0.0) * stale_penalty)
            )
        else:
            rebuilt.append(replace(r, metadata=new_meta))
    return rebuilt


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
