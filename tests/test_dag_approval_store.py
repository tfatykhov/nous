"""Harness Phase 3: approval columns and the card / deadline predicates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3store-{uuid.uuid4().hex[:8]}", _settings())


async def _one_node(store):
    dag = await store.create(
        DAGCreateRequest(
            name="one", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")]
        )
    )
    return dag, dag.nodes[0]


async def test_answer_columns_round_trip(store):
    dag, node = await _one_node(store)
    at = datetime.now(UTC)

    assert await store.transition_node(
        node.id, from_statuses={"ready"}, status="awaiting_input",
        answer_deadline=at + timedelta(hours=1), surface_id="card-1",
    )
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, status="completed",
        answer="send", answered_by="unattributed", answered_at=at,
        answer_source="companion", answer_history=[{"answer": "hold"}],
    )
    got = (await store.get_dag(dag.id)).nodes[0]
    assert (got.status, got.answer, got.answer_source) == ("completed", "send", "companion")
    assert got.answer_history == [{"answer": "hold"}]


async def test_answer_source_is_checked(store):
    _, node = await _one_node(store)
    with pytest.raises(IntegrityError):
        await store.update_node(node.id, answer_source="human")


async def test_card_predicate(store):
    _, node = await _one_node(store)
    await store.transition_node(node.id, from_statuses={"ready"}, status="awaiting_input")

    # Unlinked: any card with the key may answer (the tap-before-link window).
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, card="card-9", surface_id="card-1"
    )
    # Linked to card-1: a different card may not.
    assert not await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, card="card-2", status="completed"
    )
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, card="card-1", status="completed"
    )


async def test_due_by_predicate(store):
    _, node = await _one_node(store)
    now = datetime.now(UTC)
    await store.transition_node(
        node.id, from_statuses={"ready"}, status="awaiting_input",
        answer_deadline=now + timedelta(hours=1),
    )
    assert not await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, due_by=now, status="failed"
    )
    await store.update_node(node.id, answer_deadline=now - timedelta(seconds=1))
    assert await store.transition_node(
        node.id, from_statuses={"awaiting_input"}, due_by=now, status="failed"
    )
