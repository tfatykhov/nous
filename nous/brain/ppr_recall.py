"""F082 — PPR (Personalized PageRank) recall leg over brain.graph_edges.

Mirrors spreading_activation.py structure: density-aware gate + async SQL
fetch + Python power-iteration seeded by existing Stage-1 retrieval hits.

Graph is treated UNDIRECTED (matches HippoRAG directed=False).  Autobehavior
exclusions from graph_constants are reused verbatim — single source of truth.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.graph_constants import autobehavior_exclusion_sql
from nous.brain.spreading_activation import compute_graph_density
from nous.config import Settings

logger = logging.getLogger(__name__)

try:
    import numpy as _np
    _NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NUMPY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Density gate
# ---------------------------------------------------------------------------


def should_use_ppr(settings: Settings, cached_density: float) -> bool:
    """Return True if PPR should activate for this agent's graph density.

    Mode resolution (mirrors spreading_activation):
    - "true"  → always on
    - "false" → always off
    - "auto"  → on when density >= threshold

    Threshold resolution for auto mode:
    - ``ppr_min_density > 0`` → use that explicit value
    - ``ppr_min_density <= 0`` → inherit ``spreading_activation_density_threshold``
      (matches the spec's "reuse spreading_activation threshold" intent and keeps
      PPR's auto gate consistent with the existing spreading activation gate)
    """
    mode = str(getattr(settings, "ppr_recall_enabled", "auto")).lower()
    if mode == "true":
        return True
    if mode == "false":
        return False
    min_density = float(getattr(settings, "ppr_min_density", 0.0))
    if min_density <= 0.0:
        min_density = float(
            getattr(settings, "spreading_activation_density_threshold", 3.0)
        )
    return cached_density >= min_density


# ---------------------------------------------------------------------------
# Pure-Python PPR computation (no DB dependency; testable in isolation)
# ---------------------------------------------------------------------------


def _build_transition(
    doubled_edges: list[tuple[str, str, float]],
    all_ids: list[str],
) -> tuple[list[int], list[int], list[float]]:
    """Row-normalise the undirected adjacency and return sparse (src, tgt, w) triples.

    ``doubled_edges`` already contains both directions of each undirected edge.
    Returns three parallel lists suitable for vectorised power-iteration.
    """
    idx: dict[str, int] = {nid: i for i, nid in enumerate(all_ids)}
    n = len(all_ids)

    # Aggregate multi-edges into row → col → total_weight
    row_totals: list[float] = [0.0] * n
    cell: dict[tuple[int, int], float] = {}
    for src, tgt, w in doubled_edges:
        s = idx.get(src)
        t = idx.get(tgt)
        if s is None or t is None:
            continue
        key = (s, t)
        cell[key] = cell.get(key, 0.0) + w
        row_totals[s] += w

    src_list: list[int] = []
    tgt_list: list[int] = []
    w_list: list[float] = []
    for (s, t), w in cell.items():
        rs = row_totals[s]
        if rs > 0.0:
            src_list.append(s)
            tgt_list.append(t)
            w_list.append(w / rs)

    return src_list, tgt_list, w_list


def _top_k(
    pr: list[float],
    node_list: list[str],
    node_types: dict[str, str],
    limit: int,
) -> list[tuple[str, str, float]]:
    """Return the top-limit nodes by PPR score, skipping zero-mass nodes."""
    scored = sorted(
        ((pr[i], node_list[i]) for i in range(len(node_list)) if pr[i] > 0.0),
        reverse=True,
    )
    return [
        (id_str, node_types.get(id_str, "unknown"), float(score))
        for score, id_str in scored[:limit]
    ]


def _run_ppr(
    edges: list[tuple[str, str, float]],
    node_types: dict[str, str],
    seeds: list[tuple[str, float]],
    damping: float,
    tolerance: float,
    max_iter: int,
    limit: int,
) -> list[tuple[str, str, float]]:
    """Power-iteration PPR.  No DB dependency — fully unit-testable.

    Algorithm (§3.3):
        PR = (1-d) * reset + d * Mᵀ · PR
    where M is the row-normalised undirected adjacency and d = damping.

    Args:
        edges: Directed edge list (already doubled for undirected).
        node_types: id_str → node_type for all nodes in the graph.
        seeds: (id_str, score) pairs forming the reset vector (unnormalised).
        damping: Teleportation damping factor d.
        tolerance: L1 convergence threshold.
        max_iter: Hard iteration cap.
        limit: Maximum number of nodes to return.

    Returns:
        Top-limit (id_str, node_type, ppr_score) triples, score descending.
    """
    if not edges or not seeds:
        return []

    # Build the full node universe (graph nodes ∪ seed nodes)
    all_ids_set: set[str] = set(node_types.keys())
    for src, tgt, _ in edges:
        all_ids_set.add(src)
        all_ids_set.add(tgt)
    for id_str, _ in seeds:
        all_ids_set.add(id_str)

    node_list = sorted(all_ids_set)  # deterministic order for reproducibility
    idx: dict[str, int] = {nid: i for i, nid in enumerate(node_list)}
    n = len(node_list)

    # Reset vector: seed scores normalised to sum-to-1
    reset_raw = [0.0] * n
    for id_str, score in seeds:
        i = idx.get(id_str)
        if i is not None:
            reset_raw[i] += max(score, 0.0)

    reset_sum = sum(reset_raw)
    if reset_sum <= 0.0:
        return []
    reset = [r / reset_sum for r in reset_raw]

    # Build transition triples
    src_list, tgt_list, w_list = _build_transition(edges, node_list)

    if not src_list:
        # Isolated graph: PPR collapses to the reset vector
        return _top_k(reset, node_list, node_types, limit)

    if _NUMPY_AVAILABLE:
        return _iterate_numpy(
            src_list, tgt_list, w_list, reset, n,
            node_list, node_types, damping, tolerance, max_iter, limit,
        )
    return _iterate_pure(
        src_list, tgt_list, w_list, reset, n,
        node_list, node_types, damping, tolerance, max_iter, limit,
    )


def _iterate_numpy(
    src_list, tgt_list, w_list, reset, n,
    node_list, node_types, damping, tolerance, max_iter, limit,
):
    """NumPy-accelerated power iteration.  O(E) per iteration, no n×n matrix."""
    import numpy as np

    edges_src = _np.array(src_list, dtype=_np.int32)
    edges_tgt = _np.array(tgt_list, dtype=_np.int32)
    edges_w = _np.array(w_list, dtype=_np.float64)
    reset_arr = _np.array(reset, dtype=_np.float64)
    d = damping

    pr = reset_arr.copy()
    for _ in range(max_iter):
        pr_new = (1.0 - d) * reset_arr.copy()
        _np.add.at(pr_new, edges_tgt, d * edges_w * pr[edges_src])
        delta = float(_np.abs(pr_new - pr).sum())
        pr = pr_new
        if delta < tolerance:
            break

    return _top_k(pr.tolist(), node_list, node_types, limit)


def _iterate_pure(
    src_list, tgt_list, w_list, reset, n,
    node_list, node_types, damping, tolerance, max_iter, limit,
):
    """Pure-Python power iteration (fallback when numpy is absent)."""
    d = damping
    contrib = list(zip(src_list, tgt_list, w_list))
    pr = list(reset)
    for _ in range(max_iter):
        pr_new = [(1.0 - d) * r for r in reset]
        for s, t, w in contrib:
            pr_new[t] += d * w * pr[s]
        delta = sum(abs(pr_new[i] - pr[i]) for i in range(n))
        pr = pr_new
        if delta < tolerance:
            break
    return _top_k(pr, node_list, node_types, limit)


# ---------------------------------------------------------------------------
# Async entry point (fetches edges from DB, then calls _run_ppr)
# ---------------------------------------------------------------------------


async def ppr_recall(
    session: AsyncSession,
    agent_id: str,
    seed_nodes: list[tuple[UUID, str, float]],
    settings: Settings,
) -> list[tuple[UUID, str, float]]:
    """Query-personalized PageRank over brain.graph_edges.

    Seeded by the top-k retrieval hits already computed in Stage 1.
    Graph is treated undirected; autobehavior-excluded relations are dropped
    via the single-source-of-truth in graph_constants.

    Args:
        session: Active SQLAlchemy async session.
        agent_id: Agent ID filter (always applied).
        seed_nodes: List of (node_id, node_type, seed_score) from Stage-1 hits.
        settings: Settings with ppr_* configuration fields.

    Returns:
        Top ppr_leg_limit nodes as (node_id, node_type, ppr_score), descending.
    """
    if not seed_nodes:
        return []

    # Single SQL pull of the excluded edge set (already indexed by agent_id).
    excl = autobehavior_exclusion_sql()
    sql = text(f"""
        SELECT source_id::text  AS src,
               source_type,
               target_id::text  AS tgt,
               target_type,
               COALESCE(weight, 1.0) AS w
        FROM brain.graph_edges
        WHERE agent_id = :agent_id
          AND {excl}
    """)
    result = await session.execute(sql, {"agent_id": agent_id})
    rows = result.all()

    if not rows:
        return []

    # Build node_types map and directed edges list
    node_types: dict[str, str] = {}
    raw_edges: list[tuple[str, str, float]] = []
    for row in rows:
        node_types.setdefault(row.src, row.source_type)
        node_types.setdefault(row.tgt, row.target_type)
        raw_edges.append((row.src, row.tgt, float(row.w)))

    # Double edges for undirected treatment (matches HippoRAG directed=False)
    doubled: list[tuple[str, str, float]] = []
    for src, tgt, w in raw_edges:
        doubled.append((src, tgt, w))
        doubled.append((tgt, src, w))

    seeds = [(str(nid), max(float(score), 0.0)) for nid, _, score in seed_nodes]

    damping = float(getattr(settings, "ppr_damping", 0.5))
    tolerance = float(getattr(settings, "ppr_tolerance", 1e-4))
    max_iter = int(getattr(settings, "ppr_max_iter", 30))
    limit = int(getattr(settings, "ppr_leg_limit", 20))

    raw = _run_ppr(doubled, node_types, seeds, damping, tolerance, max_iter, limit)
    return [(UUID(id_str), ntype, score) for id_str, ntype, score in raw]
