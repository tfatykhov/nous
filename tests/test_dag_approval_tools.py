"""Harness Phase 3 §3.13-§3.14: the approval node through the agent's tools."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.store import DAGStore

APPROVAL = {
    "name": "approve", "type": "approval", "instructions": "Send the drafted email?",
    "options": [
        {"id": "send", "label": "Send it", "outcome": "proceed"},
        {"id": "hold", "label": "Don't send", "outcome": "stop"},
    ],
    "default_option": "hold",
}
SEND = {"name": "send", "type": "subtask", "instructions": "send"}
EDGES = [{"from_node": "approve", "to_node": "send", "edge_type": "context_flow"}]


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


class _Cards:
    """Just enough SurfaceService for a launch to park and link."""

    async def close_by_dedup_key(self, key, status="expired"):
        return []

    async def close(self, surface_id, status="expired"):
        # Present so a path that closes a card is exercised, not swallowed:
        # _close_card catches exceptions, so a missing method would pass silently.
        return None

    async def live_ids(self, surface_ids):
        return set(surface_ids)

    async def live_cards_by_prefix(self, prefix):
        return []

    async def push_built(self, built, **_):
        return "card-1"


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3tools-{uuid.uuid4().hex[:8]}", _settings())


def _tools(store, settings, *, wired=True):
    subtasks = AsyncMock()
    subtasks.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    orch = DAGOrchestrator(
        store=store, subtask_mgr=subtasks, dynamic_loader=AsyncMock(), settings=settings,
        surface_service=_Cards() if wired else None,
    )
    orch.clock_wired = True
    dispatcher = ToolDispatcher()
    register_dag_tools(dispatcher, store, orch, settings=settings)
    return dispatcher


def _text(result) -> str:
    return result["content"][0]["text"]


async def test_the_schema_advertises_approval_only_when_enabled(store):
    off = _tools(store, _settings())
    on = _tools(store, _settings(dag_approval_nodes_enabled=True))
    item = lambda d: d._schemas["dag_create"]["properties"]["nodes"]["items"]  # noqa: E731
    assert "approval" not in item(off)["properties"]["type"]["enum"]
    assert "options" not in item(off)["properties"]
    assert "approval" in item(on)["properties"]["type"]["enum"]
    assert {"options", "default_option", "recommended_option", "answer_timeout_seconds"} <= set(
        item(on)["properties"]
    )


async def test_dag_create_refuses_approval_nodes_while_the_flag_is_off(store):
    result = await _tools(store, _settings())._handlers["dag_create"](
        name="m", nodes=[APPROVAL, SEND], edges=EDGES
    )
    assert "NOUS_DAG_APPROVAL_NODES_ENABLED" in _text(result)


async def test_dag_create_refuses_approval_nodes_without_the_companion(store):
    result = await _tools(store, _settings(dag_approval_nodes_enabled=True), wired=False)._handlers[
        "dag_create"
    ](name="m", nodes=[APPROVAL, SEND], edges=EDGES)
    assert "companion" in _text(result)


async def test_dag_create_threads_the_approval_fields(store):
    settings = _settings(dag_approval_nodes_enabled=True)
    dispatcher = _tools(store, settings)

    result = await dispatcher._handlers["dag_create"](name="m", nodes=[APPROVAL, SEND], edges=EDGES)

    assert "Created DAG" in _text(result)
    assert "NOUS_A2UI_PUBLIC_BASE_URL is unset" in _text(result)
    dag = (await store.get_active_dags())[0]
    node = next(n for n in dag.nodes if n.name == "approve")
    assert node.approval_spec["default_option"] == "hold"
    assert node.status == "awaiting_input"


async def test_dag_manage_shows_who_is_waiting(store):
    settings = _settings(dag_approval_nodes_enabled=True, a2ui_public_base_url="https://n.example")
    dispatcher = _tools(store, settings)
    await dispatcher._handlers["dag_create"](name="m", nodes=[APPROVAL, SEND], edges=EDGES)
    dag = (await store.get_active_dags())[0]

    listing = _text(await dispatcher._handlers["dag_manage"](action="list"))
    status = _text(await dispatcher._handlers["dag_manage"](action="status", dag_id=str(dag.id)))

    assert "waiting on you: approve" in listing
    assert "[?] approve (approval, w0) — awaiting_input" in status
    assert "waiting for an answer until" in status
    assert "card: https://n.example/companion#/s/card-1" in status
