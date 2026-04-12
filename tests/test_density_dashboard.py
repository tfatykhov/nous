"""Tests for F040 graph density dashboard query function."""

from __future__ import annotations

import uuid

import pytest

from sqlalchemy import text


@pytest.mark.asyncio
async def test_get_density_data_empty(db):
    """Empty agent returns all-zero structure."""
    from nous.api.dashboard_queries import get_density_data

    agent_id = f"test-density-{uuid.uuid4().hex[:8]}"
    async with db.session() as session:
        data = await get_density_data(session, agent_id)

    assert data["total_nodes"] == 0
    assert data["total_edges"] == 0
    assert data["total_orphans"] == 0
    assert data["orphan_rate"] == 0.0
    assert data["avg_degree"] == 0.0
    assert data["connected_nodes"] == 0
    assert data["density_by_type"] is not None
    for type_name in ("fact", "decision", "episode", "procedure"):
        entry = data["density_by_type"][type_name]
        assert entry["total"] == 0
        assert entry["orphan"] == 0
        assert entry["orphan_rate"] == 0.0
    assert data["edge_distribution"] == {}
    assert data["backfill_progress"] == []


@pytest.mark.asyncio
async def test_get_density_data_with_data(db):
    """Density data reflects inserted nodes and edges."""
    from nous.api.dashboard_queries import get_density_data

    agent_id = f"test-density-{uuid.uuid4().hex[:8]}"

    async with db.session() as session:
        # Insert two facts (one will be orphan, one connected)
        fact_id_1 = uuid.uuid4()
        fact_id_2 = uuid.uuid4()
        await session.execute(
            text("""
                INSERT INTO heart.facts (id, agent_id, content, category, active)
                VALUES (:id1, :aid, 'fact one', 'general', true),
                       (:id2, :aid, 'fact two', 'general', true)
            """),
            {"id1": fact_id_1, "id2": fact_id_2, "aid": agent_id},
        )

        # Insert a decision
        dec_id = uuid.uuid4()
        await session.execute(
            text("""
                INSERT INTO brain.decisions (id, agent_id, description, confidence, stakes, category)
                VALUES (:id, :aid, 'test decision', 0.8, 'low', 'tooling')
            """),
            {"id": dec_id, "aid": agent_id},
        )

        # Link fact_1 to decision via edge
        edge_id = uuid.uuid4()
        await session.execute(
            text("""
                INSERT INTO brain.graph_edges
                    (id, agent_id, source_id, source_type, target_id, target_type, relation, auto_linked)
                VALUES (:eid, :aid, :src, 'fact', :tgt, 'decision', 'evidence_for', true)
            """),
            {"eid": edge_id, "aid": agent_id, "src": fact_id_1, "tgt": dec_id},
        )
        await session.commit()

    async with db.session() as session:
        data = await get_density_data(session, agent_id)

    # 2 facts + 1 decision = 3 nodes
    assert data["total_nodes"] == 3
    assert data["total_edges"] == 1
    # fact_2 is orphan, decision and fact_1 are connected
    assert data["total_orphans"] == 1
    assert data["connected_nodes"] == 2
    assert data["avg_degree"] == 0.5  # 1 edge / 2 connected nodes

    # Fact type: 2 total, 1 orphan
    assert data["density_by_type"]["fact"]["total"] == 2
    assert data["density_by_type"]["fact"]["orphan"] == 1

    # Decision type: 1 total, 0 orphan
    assert data["density_by_type"]["decision"]["total"] == 1
    assert data["density_by_type"]["decision"]["orphan"] == 0

    # Edge distribution
    assert data["edge_distribution"]["evidence_for"] == 1

    # Backfill progress should have today's entry
    assert len(data["backfill_progress"]) >= 1
    assert data["backfill_progress"][0]["edges"] == 1
