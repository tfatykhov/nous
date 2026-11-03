"""The hybrid vector leg sees past the default HNSW horizon.

pgvector applies WHERE filters AFTER the HNSW index walk. At the default
``hnsw.ef_search`` (40) a filtered ``ORDER BY embedding <=> q LIMIT n`` returns
only the filter's survivors among the ~40 nearest rows, so an agent's active
facts that sit behind closer inactive rows and other agents' rows never reach
the vector leg. ``hybrid_search`` therefore widens the horizon with
``SET LOCAL``, which lasts to the end of the transaction.

The seed puts every inactive row of the agent and every row of a second agent
closer to the query than any of the agent's active rows. Geometry: each vector
lies at angle ``theta`` from the query ``e1``, with its azimuth in the
``(e2, e3)`` plane spread by the golden angle, so the points cover a spherical
cap and the HNSW graph stays navigable (a degenerate layout with every row on
its own orthogonal axis leaves rows unreachable, which tests something else).

The last two tests cover the other side of ``SET LOCAL``: the settings last for
the rest of the transaction. A retrieval leg also caps ``hnsw.max_scan_tuples``,
and the graph densifier runs a hybrid search and then, in the same transaction,
a threshold-gated linking query that must keep pgvector's default horizon.
"""

from __future__ import annotations

import math
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker
from nous.heart.search import (
    VECTOR_LEG_EF_SEARCH,
    VECTOR_LEG_MAX_SCAN_TUPLES,
    hybrid_search,
    reset_local_ef_search,
    set_local_ef_search,
)
from nous.storage.models import Fact, GraphEdge

DIM = 1536
GOLDEN = math.pi * (3 - math.sqrt(5))


def _vec(theta: float, k: int) -> str:
    """A unit pgvector literal at angle ``theta`` from e1, azimuth ``k * GOLDEN``."""
    phi = k * GOLDEN
    v = [0.0] * DIM
    v[0] = math.cos(theta)
    v[1] = math.sin(theta) * math.cos(phi)
    v[2] = math.sin(theta) * math.sin(phi)
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def _insert(session, agent: str, active: bool, theta: float, k: int) -> UUID:
    fid = uuid4()
    await session.execute(
        text(
            "INSERT INTO heart.facts (id, agent_id, content, active, embedding) "
            "VALUES (:id, :a, :c, :act, CAST(:v AS vector))"
        ),
        {"id": fid, "a": agent, "c": f"horizon fact {k}", "act": active, "v": _vec(theta, k)},
    )
    return fid


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_hybrid_vector_leg_returns_full_active_set_behind_closer_filtered_rows(session):
    agent = f"hnsw-a-{uuid4().hex[:8]}"
    other = f"hnsw-b-{uuid4().hex[:8]}"

    # 200 facts for the agent, 40% active. Active rows: theta 0.5 .. 1.5.
    # Inactive rows: theta 0.05 .. 0.45, all closer to the query.
    active_by_theta: list[tuple[float, UUID]] = []
    k = 0
    for i in range(200):
        k += 1
        is_active = i % 5 < 2
        theta = 0.5 + 0.005 * i if is_active else 0.05 + 0.002 * i
        fid = await _insert(session, agent, is_active, theta, k)
        if is_active:
            active_by_theta.append((theta, fid))
    # 100 active rows of a second agent, also closer than any active row above.
    for i in range(100):
        k += 1
        await _insert(session, other, True, 0.05 + 0.003 * i, k)

    expected = {fid for _, fid in sorted(active_by_theta)[:60]}
    assert len(active_by_theta) == 80 and len(expected) == 60

    # On a table this small the planner takes the agent_id btree plus an exact
    # Sort; a prod-sized table walks idx_facts_embedding instead. Price the
    # Sort plan out so the test runs the HNSW plan, and prove it does.
    await session.execute(text("SET LOCAL enable_sort = off"))
    q = _vec(0.0, 0)
    plan = (
        await session.execute(
            text(
                "EXPLAIN SELECT t.id FROM heart.facts t "
                "WHERE t.embedding IS NOT NULL AND t.agent_id = :a AND t.active = true "
                "ORDER BY t.embedding <=> CAST(:q AS vector) LIMIT 60"
            ),
            {"a": agent, "q": q},
        )
    ).all()
    plan_text = "\n".join(row[0] for row in plan)
    assert "idx_facts_embedding" in plan_text, plan_text

    # limit=20 expands to a 60-row vector leg. The query text matches nothing,
    # so the keyword leg is empty and every fetched id came from the vector
    # leg: it is either returned or recorded in dropped_out with a vector rank.
    dropped: list = []
    kept = await hybrid_search(
        session,
        "heart.facts",
        [1.0] + [0.0] * (DIM - 1),
        "zzqxv",
        agent,
        limit=20,
        dropped_out=dropped,
    )
    vector_leg = {doc_id for doc_id, _ in kept} | {d[0] for d in dropped if d[1] is not None}

    assert len(vector_leg) == 60, f"vector leg returned {len(vector_leg)} of 60 rows"
    assert vector_leg == expected


async def _hnsw_settings(session) -> tuple[str, str, str]:
    """(ef_search, iterative_scan, max_scan_tuples) as SHOW reports them."""
    names = ("hnsw.ef_search", "hnsw.iterative_scan", "hnsw.max_scan_tuples")
    return tuple([(await session.execute(text(f"SHOW {n}"))).scalar() for n in names])


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_retrieval_leg_caps_max_scan_tuples_for_the_rest_of_the_transaction(session):
    """A retrieval leg leaves its settings, including the max_scan_tuples cap,
    in place for the rest of the transaction. A reset, or a helper call that
    passes no cap, puts the default back (pgvector: 40, off, 20000)."""
    agent = f"hnsw-cap-{uuid4().hex[:8]}"
    query = [1.0] + [0.0] * (DIM - 1)

    await hybrid_search(session, "heart.facts", query, "zzqxv", agent, limit=5)
    assert await _hnsw_settings(session) == (
        str(VECTOR_LEG_EF_SEARCH),
        "strict_order",
        str(VECTOR_LEG_MAX_SCAN_TUPLES),
    )

    await reset_local_ef_search(session)
    assert await _hnsw_settings(session) == ("40", "off", "20000")

    # An uncapped caller (fact dedup, contradiction probes) after a capped
    # retrieval leg in the same transaction scans to the default again.
    await hybrid_search(session, "heart.facts", query, "zzqxv", agent, limit=5)
    await set_local_ef_search(session, 100)
    assert await _hnsw_settings(session) == ("100", "strict_order", "20000")


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_densifier_cross_type_query_runs_at_default_horizon_after_hybrid_search(
    db, settings, mock_embeddings, monkeypatch
):
    """``backfill_orphan_facts`` runs ``_backfill_same_type`` (a hybrid search,
    which widens the horizon) and then ``_backfill_cross_type`` in one
    transaction. The cross-type threshold query must see pgvector's defaults
    (ef_search 40, iterative scan off, max_scan_tuples 20000), or this change
    would alter which graph edges get created."""
    agent = f"hnsw-dens-{uuid4().hex[:8]}"
    linker = GraphLinker(db, mock_embeddings, settings, agent)
    densifier = GraphDensifier(db, linker, mock_embeddings, settings, agent)

    # One orphan fact with an embedding, so the same-type leg runs the hybrid
    # vector search and the cross-type leg embeds and runs its threshold query.
    async with db.session() as s:
        await _insert(s, agent, True, 0.3, 1)
        await s.commit()

    seen: list[tuple[str, str, str]] = []
    original_execute = AsyncSession.execute

    async def _spy_execute(self, statement, *args, **kwargs):
        if ":pre_threshold" in str(statement):
            ef = (await original_execute(self, text("SHOW hnsw.ef_search"))).scalar()
            it = (await original_execute(self, text("SHOW hnsw.iterative_scan"))).scalar()
            mst = (await original_execute(self, text("SHOW hnsw.max_scan_tuples"))).scalar()
            seen.append((ef, it, mst))
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _spy_execute)
    try:
        await densifier.backfill_orphan_facts(max_count=1)
    finally:
        monkeypatch.undo()
        async with db.session() as s:
            for model in (GraphEdge, Fact):
                await s.execute(delete(model).where(model.agent_id == agent))
            await s.commit()

    assert seen == [("40", "off", "20000")]
