"""Tests for F038 DAG Orchestration dashboard query function."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore


@pytest.mark.asyncio
async def test_get_dag_dashboard_data_empty(db):
    from nous.api.dashboard_queries import get_dag_dashboard_data

    agent_id = f"test-dag-dash-{uuid.uuid4().hex[:8]}"
    async with db.session() as session:
        data = await get_dag_dashboard_data(session, agent_id)
    assert data["stats"]["active_count"] == 0
    assert data["active_dags"] == []
    assert data["recent_dags"] == []
    # F090.4: get_dag_dashboard_data must delegate to get_dag_phase2_signals
    # and surface its result — this is the metric's only delivery surface.
    assert "phase2_signals" in data
    assert data["phase2_signals"]["sibling_pairs"] == 0
    assert data["phase2_signals"]["callback_nodes"] == 0
    assert data["phase2_signals"]["gate_nodes"] == 0


@pytest.mark.asyncio
async def test_get_dag_dashboard_data_with_dag(db):
    agent_id = f"test-dag-dash-{uuid.uuid4().hex[:8]}"
    store = DAGStore(db, agent_id=agent_id, settings=Settings())
    req = DAGCreateRequest(
        name="dashboard-test",
        nodes=[
            DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="A"),
            DAGNodeSpec(name="b", type=DAGNodeType.check, instructions="B"),
        ],
        edges=[DAGEdgeSpec(from_node="a", to_node="b")],
    )
    dag = await store.create(req)
    await store.update_dag_status(dag.id, "running")

    from nous.api.dashboard_queries import get_dag_dashboard_data

    async with db.session() as session:
        data = await get_dag_dashboard_data(session, agent_id)

    assert data["stats"]["active_count"] == 1
    assert len(data["active_dags"]) == 1
    assert data["active_dags"][0]["name"] == "dashboard-test"
    assert len(data["active_dags"][0]["nodes"]) == 2
    assert len(data["active_dags"][0]["edges"]) == 1


@pytest.mark.asyncio
async def test_get_dag_dashboard_recent_dags(db):
    """Completed DAGs appear in recent_dags, not active_dags."""
    agent_id = f"test-dag-dash-{uuid.uuid4().hex[:8]}"
    store = DAGStore(db, agent_id=agent_id, settings=Settings())
    req = DAGCreateRequest(
        name="completed-dag",
        nodes=[
            DAGNodeSpec(name="task1", type=DAGNodeType.subtask, instructions="Do it"),
        ],
    )
    dag = await store.create(req)
    await store.update_dag_status(dag.id, "running")
    await store.update_dag_status(dag.id, "completed")

    from nous.api.dashboard_queries import get_dag_dashboard_data

    async with db.session() as session:
        data = await get_dag_dashboard_data(session, agent_id)

    assert data["stats"]["active_count"] == 0
    assert len(data["active_dags"]) == 0
    assert len(data["recent_dags"]) == 1
    assert data["recent_dags"][0]["name"] == "completed-dag"
    assert data["recent_dags"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_get_dag_dashboard_stats_success_rate(db):
    """Success rate is computed from completed vs total finished."""
    agent_id = f"test-dag-dash-{uuid.uuid4().hex[:8]}"
    store = DAGStore(db, agent_id=agent_id, settings=Settings())

    # Create and complete 2 DAGs, fail 1
    for i, status in enumerate(["completed", "completed", "failed"]):
        req = DAGCreateRequest(
            name=f"dag-{i}",
            nodes=[
                DAGNodeSpec(name="t", type=DAGNodeType.subtask, instructions="X"),
            ],
        )
        dag = await store.create(req)
        await store.update_dag_status(dag.id, "running")
        await store.update_dag_status(dag.id, status)

    from nous.api.dashboard_queries import get_dag_dashboard_data

    async with db.session() as session:
        data = await get_dag_dashboard_data(session, agent_id)

    # 2 completed out of 3 finished = 0.667
    assert data["stats"]["success_rate"] == pytest.approx(0.667, abs=0.01)
    assert data["stats"]["active_count"] == 0
    assert len(data["recent_dags"]) == 3


@pytest.mark.asyncio
async def test_get_dag_dashboard_node_and_edge_fields(db):
    """Active DAG nodes and edges have correct field structure."""
    agent_id = f"test-dag-dash-{uuid.uuid4().hex[:8]}"
    store = DAGStore(db, agent_id=agent_id, settings=Settings())
    req = DAGCreateRequest(
        name="field-test",
        nodes=[
            DAGNodeSpec(name="alpha", type=DAGNodeType.subtask, instructions="A"),
            DAGNodeSpec(name="beta", type=DAGNodeType.subtask, instructions="B"),
        ],
        edges=[DAGEdgeSpec(from_node="alpha", to_node="beta")],
    )
    dag = await store.create(req)
    await store.update_dag_status(dag.id, "running")

    from nous.api.dashboard_queries import get_dag_dashboard_data

    async with db.session() as session:
        data = await get_dag_dashboard_data(session, agent_id)

    assert len(data["active_dags"]) == 1
    dag_data = data["active_dags"][0]

    # Verify dag_id is a string
    assert isinstance(dag_data["id"], str)

    # Verify nodes have expected fields
    assert len(dag_data["nodes"]) == 2
    node = dag_data["nodes"][0]
    assert "id" in node
    assert "name" in node
    assert "status" in node
    assert "wave" in node

    # Verify edges have expected fields
    assert len(dag_data["edges"]) == 1
    edge = dag_data["edges"][0]
    assert "from_node_id" in edge
    assert "to_node_id" in edge
