"""Harness Phase 3 prerequisites: predecessor edges and conditional writes.

Pre-existing defects the approval node depends on (spec §3.3, §3.8):
failure propagation and retry's unblock followed `dependency` edges only
while readiness also waits on `context_flow`, and several status writes
were blind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


def _settings(**overrides) -> Settings:
    """Hermetic settings — never inherit the developer's .env."""
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3pre-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    mgr.get.return_value = None
    return mgr


def _orch(store, subtask_mgr) -> DAGOrchestrator:
    orch = DAGOrchestrator(
        store=store, subtask_mgr=subtask_mgr, dynamic_loader=AsyncMock(), settings=_settings()
    )
    orch.clock_wired = True
    return orch


def _two_node(edge_type: str) -> DAGCreateRequest:
    return DAGCreateRequest(
        name=f"p3-{edge_type}",
        nodes=[
            DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="draft it"),
            DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send it"),
        ],
        edges=[DAGEdgeSpec(from_node="draft", to_node="send", edge_type=edge_type)],
    )


async def _node(store, dag_id, name):
    dag = await store.get_dag(dag_id)
    return next(n for n in dag.nodes if n.name == name)


async def test_failed_node_blocks_its_context_flow_only_successor(store, subtask_mgr):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("context_flow"))
    await store.update_dag_status(dag.id, "running")
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="failed", error="boom")

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "send")).status == "blocked"
    assert (await store.get_dag(dag.id)).status == "failed"


async def test_retry_unblocks_a_context_flow_only_successor_and_clears_started_at(
    store, subtask_mgr
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("context_flow"))
    await store.update_dag_status(dag.id, "running")
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="failed", error="boom")
    await orch._advance_dag(await store.get_dag(dag.id))
    send = await _node(store, dag.id, "send")
    # A node that ran in an earlier attempt carries started_at; the stale-ready
    # sweep ignores a node that still has it (spec §3.9, §5).
    await store.update_node(send.id, started_at=datetime.now(UTC))

    await orch.retry_node(dag.id, "draft")

    send = await _node(store, dag.id, "send")
    assert send.status == "pending"
    assert send.started_at is None
    assert send.completed_at is None


async def test_a_downstream_node_left_ready_by_a_crash_is_recovered(db, store, subtask_mgr):
    """Why the unblock must clear started_at: _recover_stale_ready_nodes only
    takes `ready` nodes with started_at IS NULL (spec §3.9, §5)."""
    from datetime import timedelta

    from sqlalchemy import update as sa_update

    from nous.storage.models import ExecutionDAG

    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("context_flow"))
    await store.update_dag_status(dag.id, "running")
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="failed", error="boom")
    await orch._advance_dag(await store.get_dag(dag.id))
    send = await _node(store, dag.id, "send")
    await store.update_node(send.id, started_at=datetime.now(UTC))
    await orch.retry_node(dag.id, "draft")
    # Crash between the dispatcher's `ready` write and the launch.
    await store.update_node(send.id, status="ready")
    async with db.session() as session:
        await session.execute(
            sa_update(ExecutionDAG)
            .where(ExecutionDAG.id == dag.id)
            .values(started_at=datetime.now(UTC) - timedelta(seconds=400))
        )
        await session.commit()

    await orch._recover_stale_ready_nodes(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "send")).status == "pending"
