"""F091: retrieval telemetry — what recall retrieved, and what it dropped.

The collector is **write-only**: the retrieval pipeline reports into it and
nothing reads back out. That is the load-bearing property — with the feature
off (``NullTrace``) there is no branch anywhere that consumes trace state, so
result shape, ordering and rendered text are byte-identical by construction.
The ``recall_deep`` snapshot test and the ``nous_eval`` contract are therefore
unaffected without needing to be reasoned about at each call site.

Organizing principle is DROP ATTRIBUTION. Listing survivors tells an operator
nothing the rendered prompt doesn't already show; the question worth answering
is "why is this fact NOT in context", which means every candidate that entered
needs a terminal disposition naming the gate that removed it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# Terminal dispositions. Every registered candidate ends with exactly one.
#
# UNACCOUNTED is deliberately not a real outcome — it is what a candidate gets
# when no site claimed it, which happens when a new filter is added without
# reporting. Defaulting to `sliced_off` instead would let that drift hide as a
# plausible-looking number; a distinct value makes it a test failure.
# RENDERED means: this item was in the prompt HANDED TO the model client for
# delivery. Deliberately not "the model demonstrably received it" — a transport
# or API failure after handoff is a delivery problem, not a retrieval one, and
# chasing it through every runner error path would couple this module to them.
# The one case explicitly excluded is a pre-turn censor block, where the prompt
# is discarded before any handoff at all: pre_turn withholds the commit there,
# so a blocked turn never claims delivery. See cognitive/layer.py.
#
# KNOWN UPPER BOUND (not fixed, stated so it is not mistaken for exact): on the
# recall_deep path the runner may apply F020 SmartCompress to the tool RESULT
# TEXT after the trace has been committed (runner.py, smart_compress), dropping
# lines from large results before the next model call. Those candidates stay
# counted `rendered`, so on compressible retrievals `n_rendered` is an upper
# bound. Reconciling would require parsing compressed text back to candidate
# ids, or threading the trace across the tool-return boundary into the runner's
# compression stage — a restructuring deliberately not taken here.
RENDERED = "rendered"
SLICED_OFF = "sliced_off"
BELOW_FLOOR = "below_floor"
FILTER_DROPPED = "filter_dropped"
BUDGET_TRUNCATED = "budget_truncated"
F071_EXCLUDED = "f071_excluded"
DEDUPED = "deduped"
SUPERSEDED = "superseded"
REPLACED_AT_MERGE = "replaced_at_merge"
TYPE_EXCLUDED = "type_excluded"
UNACCOUNTED = "unaccounted"

DISPOSITIONS = frozenset({
    RENDERED, SLICED_OFF, BELOW_FLOOR, FILTER_DROPPED, BUDGET_TRUNCATED,
    F071_EXCLUDED, DEDUPED, SUPERSEDED, REPLACED_AT_MERGE, TYPE_EXCLUDED,
    UNACCOUNTED,
})

_DEFAULT_SNIPPET_CHARS = 200
_DEFAULT_MAX_CANDIDATES = 300
_DEFAULT_QUERY_CHARS = 500


@dataclass
class Mutation:
    """One score change applied to a candidate by a named stage."""

    stage: str
    score_before: float | None
    score_after: float | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "reason": self.reason,
        }


@dataclass
class Candidate:
    """One item that entered retrieval, and what became of it."""

    id: str
    type: str
    entry_leg: str
    entry_score: float | None = None
    entry_rank: int | None = None
    snippet: str = ""
    mutations: list[Mutation] = field(default_factory=list)
    final_rank: int | None = None
    disposition: str = UNACCOUNTED
    disposition_stage: str | None = None
    # Set when a gate dropped this candidate but a later stage brought it back
    # (F083 fact pinning is the live example: `_reinsert_pinned` exists
    # precisely to rescue items past diversity/dedup/relevance demotion). The
    # drop really happened, so discarding it would hide which gate the rescue
    # was needed for — but the item DID reach the model, so `disposition` must
    # say `rendered` or the accounting lies about what the prompt contained.
    restored_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "entry_leg": self.entry_leg,
            "entry_score": self.entry_score,
            "entry_rank": self.entry_rank,
            "snippet": self.snippet,
            "mutations": [m.to_dict() for m in self.mutations],
            "final_rank": self.final_rank,
            "disposition": self.disposition,
            "disposition_stage": self.disposition_stage,
            "restored_from": self.restored_from,
        }


@dataclass
class Leg:
    """One retrieval leg's summary. ``attempted`` distinguishes a leg that ran
    and found nothing from one that never ran — which no combination of config
    flags can answer, since some legs are skipped based on runtime state."""

    name: str
    attempted: bool = True
    n_returned: int = 0
    # Candidates this leg surfaced that another leg had already found.
    # Corroboration, NOT a drop — the item is still in the result set under
    # its first leg, so it must not be attributed as a loss.
    n_deduped: int = 0
    score_min: float | None = None
    score_max: float | None = None
    error: str | None = None
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "attempted": self.attempted,
            "n_returned": self.n_returned,
            "n_deduped": self.n_deduped,
            "score_min": self.score_min,
            "score_max": self.score_max,
            "error": self.error,
            "skip_reason": self.skip_reason,
        }


@dataclass
class Expansion:
    """One seed -> edge -> neighbor traversal.

    Every field here already exists on ``NeighborResult`` at the moment the
    pipeline consumes it, so recording an expansion costs no extra query.
    """

    seed_id: str
    seed_type: str
    neighbor_id: str
    neighbor_type: str
    stage: str
    seed_score: float | None = None
    hop: int = 1
    edge_relation: str | None = None
    edge_weight: float | None = None
    extraction_method: str | None = None
    # ``seed_score * edge_weight`` — the strength of THIS traversal path, NOT
    # the score the pipeline ranks the neighbour by. Those differ: with
    # ``graph_neighbor_seed_score_enabled`` off (the prod default) the ranking
    # scorer drops the seed term entirely and applies a provenance penalty, and
    # with it on it clamps weight at 1.0. Naming this "score" invited exactly
    # that confusion, so it says what it is.
    path_strength: float | None = None
    # Resolved in ``finalize`` by comparing path_strength across every
    # traversal that reached the same neighbour — NOT at record time, where
    # first-arrival is all that is knowable and a later, stronger path can
    # still win. Recording it eagerly reported the loser as the winner.
    won_best_path: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_id": self.seed_id,
            "seed_type": self.seed_type,
            "neighbor_id": self.neighbor_id,
            "neighbor_type": self.neighbor_type,
            "stage": self.stage,
            "seed_score": self.seed_score,
            "hop": self.hop,
            "edge_relation": self.edge_relation,
            "edge_weight": self.edge_weight,
            "extraction_method": self.extraction_method,
            "path_strength": self.path_strength,
            "won_best_path": self.won_best_path,
        }


def _key(item_id: Any, item_type: str) -> tuple[str, str]:
    return (str(item_id), item_type)


class RetrievalTrace:
    """Mutable per-retrieval collector.

    Not thread-safe and not shared: one instance per retrieval call, carried on
    a ContextVar so concurrent turns each get their own (ContextVar is
    per-asyncio-Task). ``to_dict`` snapshots it so the fire-and-forget DB write
    never holds a reference to an object the pipeline can still mutate.
    """

    def __init__(
        self,
        query: str = "",
        path: str = "pipeline",
        agent_id: str = "",
        session_id: str | None = None,
        turn_number: int | None = None,
        trace_id: str | None = None,
        snippet_chars: int = _DEFAULT_SNIPPET_CHARS,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
        capture_candidates: bool = True,
        query_chars: int = _DEFAULT_QUERY_CHARS,
    ):
        self.id = uuid4().hex[:16]
        # Truncated at construction, not at render: on the context path this is
        # the raw user message, and an untruncated copy would sit in a
        # diagnostics table for the full retention window. The query is a label
        # for finding the retrieval again, not a record of what the user said.
        self.query = (query or "")[:query_chars] if query_chars > 0 else (query or "")
        self.path = path
        self.agent_id = agent_id
        self.session_id = session_id
        self.turn_number = turn_number
        self.trace_id = trace_id
        self.duration_ms: float | None = None

        self._snippet_chars = snippet_chars
        self._max_candidates = max_candidates
        self._capture_candidates = capture_candidates

        self._legs: dict[str, Leg] = {}
        self._candidates: dict[tuple[str, str], Candidate] = {}
        self._expansions: list[Expansion] = []
        self._excluded_types: list[tuple[str, str]] = []
        self._truncated = False

    # -- legs ---------------------------------------------------------------

    def leg(
        self,
        name: str,
        attempted: bool = True,
        n_returned: int | None = None,
        n_deduped: int | None = None,
        error: str | None = None,
        skip_reason: str | None = None,
        scores: list[float] | None = None,
    ) -> None:
        """Record (or update) one leg's summary. Idempotent per name."""
        entry = self._legs.get(name)
        if entry is None:
            entry = Leg(name=name, attempted=attempted)
            self._legs[name] = entry
        entry.attempted = entry.attempted or attempted
        if n_returned is not None:
            entry.n_returned = n_returned
        if n_deduped is not None:
            entry.n_deduped = n_deduped
        if error is not None:
            entry.error = error
        if skip_reason is not None:
            entry.skip_reason = skip_reason
        if scores:
            valid = [s for s in scores if s is not None]
            if valid:
                lo, hi = min(valid), max(valid)
                entry.score_min = lo if entry.score_min is None else min(entry.score_min, lo)
                entry.score_max = hi if entry.score_max is None else max(entry.score_max, hi)

    # -- candidates ---------------------------------------------------------

    def add(
        self,
        item_id: Any,
        item_type: str,
        leg: str,
        score: float | None = None,
        rank: int | None = None,
        content: str | None = None,
    ) -> None:
        """Register a candidate at its point of entry.

        First leg to report an id wins ``entry_leg`` — a later leg surfacing
        the same id is corroboration, and the pipeline records that separately
        as a ``deduped`` drop of the later copy.
        """
        if not self._capture_candidates:
            return
        k = _key(item_id, item_type)
        if k in self._candidates:
            return
        if len(self._candidates) >= self._max_candidates:
            if not self._truncated:
                self._truncated = True
                logger.warning(
                    "F091: retrieval trace hit max_candidates=%d for query=%r "
                    "(further candidates not recorded)",
                    self._max_candidates, self.query[:80],
                )
            return
        # Coerce rather than assume: producers hand us whatever their result
        # object carries in a description/summary/content field, and a
        # non-str there would raise INSIDE the retrieval hot path. Telemetry
        # must never break the thing it observes, so the one place untrusted
        # values enter is the one place that has to be defensive.
        if content is None:
            snippet = ""
        elif isinstance(content, str):
            snippet = content
        else:
            snippet = str(content)

        self._candidates[k] = Candidate(
            id=str(item_id),
            type=item_type,
            entry_leg=leg,
            entry_score=score,
            entry_rank=rank,
            snippet=snippet[: self._snippet_chars],
        )

    def add_many(self, items: list, leg: str, *, type_of, score_of, content_of=None) -> None:
        """Bulk ``add`` with rank assigned by position."""
        if not self._capture_candidates:
            return
        for i, item in enumerate(items):
            self.add(
                item_id=getattr(item, "id", item),
                item_type=type_of(item),
                leg=leg,
                score=score_of(item),
                rank=i + 1,
                content=content_of(item) if content_of else None,
            )

    def mutate(
        self,
        item_id: Any,
        item_type: str,
        stage: str,
        before: float | None,
        after: float | None,
        reason: str | None = None,
    ) -> None:
        cand = self._candidates.get(_key(item_id, item_type))
        if cand is not None:
            cand.mutations.append(Mutation(stage, before, after, reason))

    def drop(self, item_id: Any, item_type: str, disposition: str, stage: str) -> None:
        """Assign a terminal disposition. First drop wins — a candidate removed
        by an early gate must not be relabelled by a later one that merely
        observes its absence."""
        cand = self._candidates.get(_key(item_id, item_type))
        if cand is None or cand.disposition != UNACCOUNTED:
            return
        cand.disposition = disposition
        cand.disposition_stage = stage

    def mark_rendered(self, item_id: Any, item_type: str, stage: str) -> None:
        """Mark an item that reached the model but was registered AFTER
        ``finalize`` (so finalize's own pass could not see it).

        Used for content appended past the ranked result set — parent-episode
        summaries, for one, which the formatter adds after the pipeline has
        already finished. Without this they are memory delivered to the model
        with no representation in the counts.

        This is an AUTHORITATIVE late render, so it OVERRIDES an earlier drop
        the same way ``finalize`` does, preserving the overridden gate on
        ``restored_from``. The concrete case: a parent episode can be a Heart
        candidate already marked ``sliced_off`` by the Heart limit, and then be
        appended as parent context because a surviving fact pointed at it. It
        genuinely reached the model, so refusing to override would leave
        delivered content counted as dropped.
        """
        cand = self._candidates.get(_key(item_id, item_type))
        if cand is None or cand.disposition == RENDERED:
            return
        if cand.disposition != UNACCOUNTED:
            cand.restored_from = f"{cand.disposition}@{cand.disposition_stage}"
        cand.disposition = RENDERED
        cand.disposition_stage = stage

    def mark_not_delivered(
        self, item_id: Any, item_type: str, disposition: str, stage: str,
    ) -> None:
        """Counterpart to ``mark_rendered``: a stage AFTER ``finalize`` removed
        something finalize had counted as delivered.

        ``drop`` deliberately refuses to touch a candidate that already has a
        disposition, so it cannot express this — the item really was in the
        ranked result set, and a later stage (the formatter's per-section scope
        gate, for one) then declined to emit it. Only downgrades from
        ``RENDERED``; anything already dropped keeps its original gate, since
        the first gate to remove an item is the true cause.
        """
        cand = self._candidates.get(_key(item_id, item_type))
        if cand is None or cand.disposition != RENDERED:
            return
        cand.disposition = disposition
        cand.disposition_stage = stage
        cand.final_rank = None

    def drop_all(self, items: list, item_type: str, disposition: str, stage: str) -> None:
        for item in items:
            self.drop(getattr(item, "id", item), item_type, disposition, stage)

    def exclude_type(self, item_type: str, stage: str) -> None:
        """A whole memory type removed from the pool before search ran.

        Distinct from a candidate drop: there are no candidates to attribute,
        because the type never got queried.
        """
        self._excluded_types.append((item_type, stage))

    # -- graph --------------------------------------------------------------

    def expansion(self, **kwargs: Any) -> None:
        seed_id = kwargs.pop("seed_id", None)
        neighbor_id = kwargs.pop("neighbor_id", None)
        if seed_id is None or neighbor_id is None:
            return
        self._expansions.append(
            Expansion(seed_id=str(seed_id), neighbor_id=str(neighbor_id), **kwargs)
        )

    # -- finalize -----------------------------------------------------------

    def finalize(self, results: list, duration_ms: float | None = None) -> None:
        """Mark what survived, and rank it.

        ``results`` is authoritative about what reached the model, so presence
        here OVERRIDES an earlier drop — a stage that resurrects a candidate
        (pinning) would otherwise leave it counted as dropped while it sits in
        the prompt. The overridden gate is preserved on ``restored_from``.

        Anything registered but neither dropped nor present here keeps
        ``UNACCOUNTED`` on purpose — see the module docstring.
        """
        self.duration_ms = duration_ms
        self._resolve_best_paths()
        if not self._capture_candidates:
            return
        for i, r in enumerate(results):
            cand = self._candidates.get(_key(getattr(r, "id", r), getattr(r, "type", "")))
            if cand is None:
                continue
            cand.final_rank = i + 1
            if cand.disposition not in (UNACCOUNTED, RENDERED):
                cand.restored_from = f"{cand.disposition}@{cand.disposition_stage}"
            cand.disposition = RENDERED
            cand.disposition_stage = "final"

    # -- output -------------------------------------------------------------

    def _resolve_best_paths(self) -> None:
        """Decide which traversal won, once every traversal is known.

        Recorded eagerly, ``won_best_path`` could only mean "first to arrive",
        which is wrong wherever the pipeline does best-composed-path
        replacement (a later, stronger path adopts the slot) and can even mark
        two paths as winners when a weak seed is visited first. Resolving here
        is order-independent: highest ``path_strength`` per (neighbour, stage)
        wins, ties break on first arrival, and None sorts last.
        """
        best: dict[tuple[str, str], int] = {}
        for i, e in enumerate(self._expansions):
            key = (e.neighbor_id, e.stage)
            cur = best.get(key)
            if cur is None or (e.path_strength or -1.0) > (
                self._expansions[cur].path_strength or -1.0
            ):
                best[key] = i
        winners = set(best.values())
        for i, e in enumerate(self._expansions):
            e.won_best_path = i in winners

    def disposition_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self._candidates.values():
            counts[c.disposition] = counts.get(c.disposition, 0) + 1
        return counts

    @property
    def n_rendered(self) -> int:
        return sum(1 for c in self._candidates.values() if c.disposition == RENDERED)

    def to_dict(self) -> dict[str, Any]:
        """Snapshot for persistence. Safe to hand to a background task."""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "turn_number": self.turn_number,
            "trace_id": self.trace_id,
            "path": self.path,
            "query": self.query,
            "duration_ms": self.duration_ms,
            "legs": [leg.to_dict() for leg in self._legs.values()],
            "excluded_types": [
                {"type": t, "stage": s} for t, s in self._excluded_types
            ],
            "n_candidates": len(self._candidates),
            "n_rendered": self.n_rendered,
            "n_expansions": len(self._expansions),
            "disposition_counts": self.disposition_counts(),
            "candidates": (
                [c.to_dict() for c in self._candidates.values()]
                if self._capture_candidates else None
            ),
            "expansions": [e.to_dict() for e in self._expansions],
            "truncated": self._truncated,
        }


class NullTrace:
    """No-op collector used when telemetry is disabled.

    Exists so instrumented call sites never guard on ``is not None``. A guard
    at every one of ~30 sites is how one missed check becomes an AttributeError
    in production; a null object makes that structurally impossible.
    ``tests`` assert this class exposes the same public methods as
    ``RetrievalTrace``, so a new capture method cannot be added to one alone.
    """

    id = ""
    enabled = False

    def leg(self, *a: Any, **k: Any) -> None: ...
    def add(self, *a: Any, **k: Any) -> None: ...
    def add_many(self, *a: Any, **k: Any) -> None: ...
    def mutate(self, *a: Any, **k: Any) -> None: ...
    def drop(self, *a: Any, **k: Any) -> None: ...
    def drop_all(self, *a: Any, **k: Any) -> None: ...
    def mark_rendered(self, *a: Any, **k: Any) -> None: ...
    def mark_not_delivered(self, *a: Any, **k: Any) -> None: ...
    def exclude_type(self, *a: Any, **k: Any) -> None: ...
    def expansion(self, *a: Any, **k: Any) -> None: ...
    def finalize(self, *a: Any, **k: Any) -> None: ...
    def disposition_counts(self) -> dict[str, int]: return {}
    def to_dict(self) -> dict[str, Any]: return {}

    @property
    def n_rendered(self) -> int: return 0


NULL_TRACE = NullTrace()

__all__ = [
    "Candidate", "Expansion", "Leg", "Mutation",
    "NULL_TRACE", "NullTrace", "RetrievalTrace",
    "DISPOSITIONS", "RENDERED", "SLICED_OFF", "BELOW_FLOOR", "FILTER_DROPPED",
    "BUDGET_TRUNCATED", "F071_EXCLUDED", "DEDUPED", "SUPERSEDED",
    "REPLACED_AT_MERGE", "TYPE_EXCLUDED", "UNACCOUNTED",
]
