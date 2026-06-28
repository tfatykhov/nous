"""F044 tinyHippo-Lite v1 — STC (Synaptic Tagging & Capture) consolidation.

Telemetry-only first slice. Implements two safe operations over
``brain.graph_edges``:

1. **Promotion gate** — a tagged edge whose cumulative reinforcement
   (``ltp_count``, the PRP analog) reaches the threshold is promoted to
   ``consolidated``. Idempotent: re-running promotes nothing new.
2. **Telemetry** — a single aggregate over the agent's edges so we can watch
   whether the reinforcement signal (re-derivation via the flag-gated upsert
   hooks) actually accumulates, or stays starved at ``ltp_count = 0``.

NOT in v1: homeostatic α-downscale, weight-floor mortality, bidirectional
replay telemetry, HDF5 logging. No weight is changed and no edge is deleted
here — this slice only sets the ``consolidation_state`` column (which no
downstream consumer reads yet) and reports counts.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Keys produced by ``stc_promote_and_measure``; used to zero-fill a partial
# result when promotion/telemetry fails after the (durable) recall flush.
_STC_PROMOTE_KEYS = (
    "f044_promoted",
    "f044_n_edges",
    "f044_n_tagged",
    "f044_n_consolidated",
    "f044_ltp_ge1",
    "f044_ltp_ge2",
    "f044_ltp_ge3",
    "f044_reinforced_24h",
)

# F044 v1.1 — buffered recall-touch reinforcement. Edges among co-retrieved
# results are "reactivated" by a recall (retrieval == STC reactivation). We
# accumulate them in a process-global buffer so the read path stays write-free,
# then flush to ltp_count at sleep. Single-process scope (the sleep handler and
# the recall path share this module in one process); cross-worker touches in a
# multi-worker prod deploy are a known v1.1 limitation.
#
# Keyed by (agent_id, source_id, target_id, relation): a single process may
# serve more than one agent, and promotion/telemetry are agent-scoped, so the
# flush must drain only the sleeping agent's touches — otherwise agent B's sleep
# would apply and clear agent A's buffered touches.
_RECALL_TOUCH_BUFFER: "Counter[tuple[str, str, str, str]]" = Counter()


def record_recall_touches(edges: list[tuple[str, str, str]], agent_id: str) -> None:
    """Buffer (source_id, target_id, relation) edges reactivated by one recall."""
    for source_id, target_id, relation in edges:
        _RECALL_TOUCH_BUFFER[(agent_id, source_id, target_id, relation)] += 1


def _recall_buffer_size() -> int:
    """Distinct buffered edges (test/telemetry helper)."""
    return len(_RECALL_TOUCH_BUFFER)


_LTP_INCREMENT_BY_SQL = text(
    """
    UPDATE brain.graph_edges
       SET ltp_count = ltp_count + :n,
           last_ltp_at = now()
     WHERE agent_id = :agent
       AND source_id = :source_id
       AND target_id = :target_id
       AND relation = :relation
       AND extraction_method IS DISTINCT FROM 'deterministic'
    """
)


async def flush_recall_touches(session: Any, agent_id: str, *, commit: bool = True) -> int:
    """Apply ``agent_id``'s buffered recall touches to ltp_count, clear its keys.

    Called at sleep (before the promotion gate). Each distinct edge is bumped by
    its buffered touch count. Only the sleeping agent's keys are drained — other
    agents' buffered touches survive for their own flush. Returns the number of
    distinct edges reinforced.

    The buffer drain is in-process and only valid if the writes commit durably,
    so the commit lives INSIDE the restore-guarded block: any failure (an
    execute OR the commit) rolls the writes back AND restores the snapshot for a
    clean re-flush. Callers that manage their own transaction/rollback isolation
    (the Postgres-lane tests) pass ``commit=False`` and read pre-rollback.
    """
    if not _RECALL_TOUCH_BUFFER:
        return 0
    items = [(key, n) for key, n in _RECALL_TOUCH_BUFFER.items() if key[0] == agent_id]
    if not items:
        return 0
    # Subtract the snapshot up front, synchronously (no await in this loop), so a
    # recall that records new touches while the writes below are suspended on an
    # await is NOT clobbered by a blanket clear() — only the concurrent remainder
    # stays in the global buffer for the next flush.
    for key, n in items:
        _RECALL_TOUCH_BUFFER[key] -= n
        if _RECALL_TOUCH_BUFFER[key] <= 0:
            del _RECALL_TOUCH_BUFFER[key]
    try:
        for (agent, source_id, target_id, relation), n in items:
            await session.execute(
                _LTP_INCREMENT_BY_SQL,
                {"agent": agent, "source_id": source_id, "target_id": target_id,
                 "relation": relation, "n": int(n)},
            )
        if commit:
            await session.commit()
    except BaseException:
        # A write OR the commit raised: the transaction rolls back, so restore
        # the snapshot for a clean re-flush (no partial double-count, no lost
        # touches — the loss window codex flagged at round 9/10).
        for key, n in items:
            _RECALL_TOUCH_BUFFER[key] += n
        raise
    return len(items)

# Cumulative-reinforcement histogram buckets reported each cycle. The whole
# bet hinges on this distribution shifting right over cycles; if edges stay at
# ltp_count = 0 the re-derivation reinforcement source is starved.
_TELEMETRY_SQL = text(
    """
    SELECT
      count(*)                                                        AS n_edges,
      count(*) FILTER (WHERE consolidation_state = 'tagged')          AS n_tagged,
      count(*) FILTER (WHERE consolidation_state = 'consolidated')    AS n_consolidated,
      count(*) FILTER (WHERE ltp_count >= 1)                          AS ltp_ge1,
      count(*) FILTER (WHERE ltp_count >= 2)                          AS ltp_ge2,
      count(*) FILTER (WHERE ltp_count >= 3)                          AS ltp_ge3,
      count(*) FILTER (
          WHERE last_ltp_at IS NOT NULL
            AND last_ltp_at > now() - interval '24 hours'
      )                                                               AS reinforced_24h
    FROM brain.graph_edges
    WHERE agent_id = :agent
    """
)

_PROMOTE_SQL = text(
    """
    UPDATE brain.graph_edges
       SET consolidation_state = 'consolidated'
     WHERE agent_id = :agent
       AND consolidation_state = 'tagged'
       AND ltp_count >= :prp
    """
)

_LTP_INCREMENT_SQL = text(
    """
    UPDATE brain.graph_edges
       SET ltp_count = ltp_count + 1,
           last_ltp_at = now()
     WHERE source_id = :source_id
       AND target_id = :target_id
       AND relation = :relation
       AND extraction_method IS DISTINCT FROM 'deterministic'
    """
)
# The deterministic guard is the structural fix for the whole reinforcement
# family: a live non-structural producer (e.g. DecisionGraphLinker `discussed_in`)
# can ON-CONFLICT onto the deterministic episode→decision edge written by
# link_episode_deterministic. Filtering at the UPDATE keeps structural anchors
# out of LTP regardless of which producer triggered the conflict — defense at the
# mutation point, not just the producer-side provenance check / read-side filter.


_DOWNSCALE_SQL = text(
    """
    UPDATE brain.graph_edges
       SET weight = weight * :alpha
     WHERE agent_id = :agent
       AND consolidation_state = 'tagged'
       AND extraction_method IS DISTINCT FROM 'deterministic'
    """
)
# `extraction_method = 'deterministic'` is the F065 structural tier (F070 chunk
# `part_of` anchors, adjacent-chunk links, supersession/episode-token edges).
# These are deterministically rebuilt and the LTP hook explicitly skips them
# (provenance_source == 'structural'), so they never reinforce → never promote →
# would decay toward zero across sleeps. Exempt them, mirroring that skip. NULL
# rows fail open to the 'heuristic' tier and remain subject to downscale.


async def homeostatic_downscale(session: Any, agent_id: str, alpha: float) -> int:
    """F044 Phase 8d (spec-faithful): multiplicatively decay TAGGED edge weights
    by ``alpha`` each sleep cycle; consolidated edges are exempt (sticky).

    This is the spec's actual mechanism for making consolidation influence
    retrieval — a *global* edge-weight change read by every weight-based graph
    consumer (spreading activation, adjacency, neighbor scoring), not a
    read-time boost in one function. Over cycles, frequently-reactivated
    (consolidated) edges become relatively dominant. Returns rows downscaled.
    """
    res = await session.execute(_DOWNSCALE_SQL, {"alpha": float(alpha), "agent": agent_id})
    return res.rowcount or 0


async def increment_ltp_on_rederivation(
    session: Any, source_id: Any, target_id: Any, relation: str
) -> None:
    """F044 reinforcement hook: bump the LTP counter of a RE-DERIVED edge.

    Called only on the ``ON CONFLICT`` (already-exists) branch of a *live*
    similarity linker — a conflict there means the same (source, target,
    relation) was independently rediscovered, i.e. reinforced. Issued as a
    separate UPDATE so the linker's ``rowcount``-based new-edge accounting is
    unchanged. Raw increment, no debounce: v1 measures the true per-cycle
    re-derivation rate, so deterministic sleep-time rebuilders are deliberately
    NOT routed here.
    """
    await session.execute(
        _LTP_INCREMENT_SQL,
        {"source_id": source_id, "target_id": target_id, "relation": relation},
    )


async def stc_promote_and_measure(
    session: Any, agent_id: str, prp_threshold: int
) -> dict[str, int]:
    """Run the promotion gate + gather telemetry on a caller-provided session.

    Promotion runs before the aggregate so the counts reflect this cycle's
    promotions. Caller owns the transaction (commit/rollback) — this lets tests
    exercise the gate under rollback isolation.
    """
    promoted = await session.execute(
        _PROMOTE_SQL, {"agent": agent_id, "prp": int(prp_threshold)}
    )
    n_promoted = promoted.rowcount or 0
    row = (
        await session.execute(_TELEMETRY_SQL, {"agent": agent_id})
    ).mappings().one()
    return {
        "f044_promoted": n_promoted,
        "f044_n_edges": int(row["n_edges"]),
        "f044_n_tagged": int(row["n_tagged"]),
        "f044_n_consolidated": int(row["n_consolidated"]),
        "f044_ltp_ge1": int(row["ltp_ge1"]),
        "f044_ltp_ge2": int(row["ltp_ge2"]),
        "f044_ltp_ge3": int(row["ltp_ge3"]),
        "f044_reinforced_24h": int(row["reinforced_24h"]),
    }


async def run_stc_consolidation(db: Any, agent_id: str, prp_threshold: int) -> dict[str, int]:
    """Run the STC promotion gate + telemetry for one sleep cycle.

    Returns a flat ``f044_*`` stats dict for merging into ``sleep_stats``.

    The flush commits in its OWN transaction (commit=True), ahead of promotion:
    the in-process buffer drain is not durable until commit, so sharing one
    transaction would lose the drained touches if promotion or its commit aborted
    (DB rolled back, buffer already cleared). Committing the flush first makes the
    touches durable; promotion is idempotent and simply re-runs next sleep if it
    fails.
    """
    async with db.session() as session:
        touched = await flush_recall_touches(session, agent_id)
    # Capture the (already-committed, durable) flush count FIRST so a later
    # promotion/telemetry failure can't discard it from the returned stats —
    # otherwise the committed ltp writes would get no audit summary (codex P2).
    # Promotion is idempotent and simply re-runs next cycle if it aborts here.
    stats: dict[str, int] = {"f044_recall_touches_flushed": touched}
    try:
        async with db.session() as session:
            stats.update(await stc_promote_and_measure(session, agent_id, prp_threshold))
            await session.commit()
    except Exception:
        logger.warning(
            "F044 STC promotion/telemetry failed after recall flush (touched=%d); "
            "reporting flush count only, promotion retries next cycle",
            touched, exc_info=True,
        )
        for k in _STC_PROMOTE_KEYS:
            stats.setdefault(k, 0)
    return stats
