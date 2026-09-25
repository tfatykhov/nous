"""Harness Phase 3 end to end: a companion tap resumes the DAG.

postgres_only: the real SurfaceService and ActionRouter
(a2ui_surfaces.allowed_actions does not round-trip on SQLite), the real
DAGStore and DAGOrchestrator. CI runs these (NOUS_TEST_DB=postgres).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from nous.a2ui.actions import ActionRouter
from nous.a2ui.service import SurfaceService
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore
from nous.storage.models import A2uiAction, A2uiSurface, ExecutionDAG

pytestmark = pytest.mark.postgres_only


@pytest_asyncio.fixture
async def world(db, settings):
    agent = f"test-p3e2e-{uuid.uuid4().hex[:10]}"
    cfg = settings.model_copy(
        update={
            "agent_id": agent, "telegram_bot_token": None, "telegram_chat_id": None,
            "dag_approval_nodes_enabled": True,
        }
    )
    surfaces = SurfaceService(db, cfg)
    store = DAGStore(db, agent, cfg)
    orch = DAGOrchestrator(
        store=store, subtask_mgr=AsyncMock(), dynamic_loader=AsyncMock(), settings=cfg,
        surface_service=surfaces,
    )
    orch.clock_wired = True
    yield SimpleNamespace(
        db=db, store=store, orch=orch, router=ActionRouter(db, cfg, surfaces, dag_orchestrator=orch)
    )
    async with db.session() as session:
        await session.execute(delete(ExecutionDAG).where(ExecutionDAG.agent_id == agent))
        await session.execute(delete(A2uiAction).where(A2uiAction.agent_id == agent))
        await session.execute(delete(A2uiSurface).where(A2uiSurface.agent_id == agent))
        await session.commit()


def _request() -> DAGCreateRequest:
    return DAGCreateRequest(
        name="mail",
        nodes=[
            DAGNodeSpec(
                name="approve", type=DAGNodeType.approval, instructions="Send it?",
                options=[
                    {"id": "send", "label": "Send it", "outcome": "proceed"},
                    {"id": "hold", "label": "Don't send", "outcome": "stop"},
                ],
                default_option="hold",
            ),
            DAGNodeSpec(name="after", type=DAGNodeType.gate),  # auto-passes: shows it launched
        ],
        edges=[DAGEdgeSpec(from_node="approve", to_node="after", edge_type="context_flow")],
    )


async def _node(world, dag_id, name):
    return next(n for n in (await world.store.get_dag(dag_id)).nodes if n.name == name)


async def _tap(world, surface_id, option):
    async with world.db.session() as session:
        nonce = (
            await session.execute(select(A2uiSurface.nonce).where(A2uiSurface.surface_id == surface_id))
        ).scalar_one()
    return await world.router.handle(
        {
            "action": {
                "name": "approval.choose", "surfaceId": surface_id,
                "context": {"optionId": option},
                "metadata": {"extensions": {"com_nous_nonce": nonce}},
            }
        },
        content_type="application/json",
    )


async def _card(world, surface_id):
    async with world.db.session() as session:
        return (
            await session.execute(select(A2uiSurface).where(A2uiSurface.surface_id == surface_id))
        ).scalar_one()


async def _started(world):
    dag = await world.store.create(_request())
    await world.orch.start_dag(dag.id)
    node = await _node(world, dag.id, "approve")
    assert node.status == "awaiting_input" and node.surface_id
    return dag, node


async def test_a_tap_resumes_the_dag(world):
    dag, node = await _started(world)

    status, body = await _tap(world, node.surface_id, "send")
    await world.orch.tick()

    assert (status, body["resolved"]) == (200, True)
    assert (await _node(world, dag.id, "after")).status == "completed"
    assert (await world.store.get_dag(dag.id)).status == "completed"


async def test_a_stop_tap_stops_the_dag(world):
    dag, node = await _started(world)

    status, _ = await _tap(world, node.surface_id, "hold")
    await world.orch.tick()

    assert status == 200
    assert (await _node(world, dag.id, "after")).status == "blocked"
    final = await world.store.get_dag(dag.id)
    assert final.status == "failed" and final.result_summary.startswith("Stopped at approval")


async def test_a_tap_between_push_and_link_is_recorded(world, monkeypatch):
    """The v1 P1, end to end: a REAL live card, not yet linked to its node,
    is tapped through the real router; the dedup key finds the node."""
    real_push = world.orch._surface_service.push_built
    taps: list[tuple] = []

    async def push_then_tap(built, **kwargs):
        surface_id = await real_push(built, **kwargs)
        if not taps:
            taps.append(await _tap(world, surface_id, "send"))
        return surface_id

    monkeypatch.setattr(world.orch._surface_service, "push_built", push_then_tap)
    dag = await world.store.create(_request())

    await world.orch.start_dag(dag.id)

    assert taps[0][0] == 200
    node = await _node(world, dag.id, "approve")
    assert (node.status, node.answer) == ("completed", "send")
    async with world.db.session() as session:
        card = (
            await session.execute(select(A2uiSurface).where(A2uiSurface.surface_id == node.surface_id))
        ).scalar_one()
    assert card.status == "resolved"


async def test_a_second_tap_is_refused_and_the_card_stays_up_until_the_sweep(world):
    dag, node = await _started(world)
    await world.orch.answer_node(node.id, "send", source="companion", actor="other-device", surface_id=node.surface_id)

    status, body = await _tap(world, node.surface_id, "hold")

    assert status == 422
    assert body["error"]["message"].startswith("already answered 'Send it'")
    card = await _card(world, node.surface_id)
    assert card.status == "live"

    await world.orch.tick()  # the leaked-card sweep retires it
    card = await _card(world, node.surface_id)
    assert card.status == "expired"
