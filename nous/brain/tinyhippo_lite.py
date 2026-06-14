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

from typing import Any

from sqlalchemy import text

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


async def run_stc_consolidation(db: Any, agent_id: str, prp_threshold: int) -> dict[str, int]:
    """Run the promotion gate + gather STC telemetry for one sleep cycle.

    Returns a flat ``f044_*`` stats dict for merging into ``sleep_stats``.
    Single short transaction; the promotion runs before the aggregate so the
    counts reflect this cycle's promotions.
    """
    async with db.session() as session:
        promoted = await session.execute(
            _PROMOTE_SQL, {"agent": agent_id, "prp": int(prp_threshold)}
        )
        n_promoted = promoted.rowcount or 0
        row = (
            await session.execute(_TELEMETRY_SQL, {"agent": agent_id})
        ).mappings().one()
        await session.commit()

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
