"""Harness dashboard routes (spec 2026-09-25 v2): status codes, shapes, and
the DAG route threading the orchestrator's held reason."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from nous.brain.brain import Brain
from nous.cognitive.layer import CognitiveLayer
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


class _Runner:
    _ledgers: dict = {}


@pytest_asyncio.fixture
async def brain(db, settings):
    b = Brain(database=db, settings=settings)
    yield b
    await b.close()


def _app(db, settings, brain, heart, **kw):
    from nous.api.rest import create_app

    cognitive = CognitiveLayer(brain, heart, settings, identity_prompt="You are Nous.")
    return create_app(_Runner(), brain, heart, cognitive, db, settings, **kw)


async def _get(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path)


@pytest.mark.parametrize("path", [
    "/dashboard/execution?window=1y",
    "/dashboard/execution?context=heartbeat",
    "/dashboard/execution?limit=abc",
    "/dashboard/harness?window=90d",
])
async def test_bad_input_is_a_400_not_a_500(db, settings, brain, heart, path):
    resp = await _get(_app(db, settings, brain, heart), path)
    assert resp.status_code == 400
    assert "error" in resp.json()


async def test_the_new_routes_answer_with_their_shapes(db, settings, brain, heart):
    app = _app(db, settings, brain, heart)

    execution = (await _get(app, "/dashboard/execution")).json()
    assert set(execution) == {"modes", "stats", "attention", "rows", "next_before"}
    assert execution["modes"]["offered_set"] == settings.tool_offered_set_enforcement_mode
    assert execution["modes"]["events_persisted"] == settings.f026_persistence_enabled
    harness = (await _get(app, "/dashboard/harness")).json()
    assert set(harness["rules"]) == {"offered_set", "context_policy", "claims"}
    attention = (await _get(app, "/dashboard/attention")).json()
    assert {"questions_waiting", "sends_in_doubt", "harness", "ledger_persisted"} <= set(attention)


async def test_the_dag_route_reads_the_orchestrators_held_reason(db, settings, brain, heart):
    store = DAGStore(db, settings.agent_id, settings)
    dag = await store.create(DAGCreateRequest(
        name="held", nodes=[DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="a")]))
    await store.update_dag_status(dag.id, "running")
    orch = SimpleNamespace(held_reason=lambda dag_id: "approved — waiting" if dag_id == dag.id else None)

    data = (await _get(_app(db, settings, brain, heart, dag_orchestrator=orch), "/dashboard/dag")).json()

    mine = next(d for d in data["active_dags"] if UUID(d["id"]) == dag.id)
    assert mine["held_reason"] == "approved — waiting"
    assert "waiting_on_you" in data and "waiting_count" in data["stats"]
