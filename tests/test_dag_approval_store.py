"""Harness Phase 3: approval columns and the card / deadline predicates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
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




def _approval_spec(name: str = "approve") -> DAGNodeSpec:
    return DAGNodeSpec(
        name=name, type=DAGNodeType.approval, instructions="Go?",
        options=[
            {"id": "go", "label": "Go", "outcome": "proceed"},
            {"id": "no", "label": "No", "outcome": "stop"},
        ],
        default_option="no",
    )


async def _parked_dag(store, *, extra=(), edges=()):
    dag = await store.create(
        DAGCreateRequest(
            name=f"parked-{uuid.uuid4().hex[:6]}",
            nodes=[
                _approval_spec(),
                DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="s"),
                *extra,
            ],
            edges=[DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"), *edges],
        )
    )
    await store.update_dag_status(dag.id, "running")
    by_name = {n.name: n for n in dag.nodes}
    await store.update_node(by_name["approve"].id, status="awaiting_input")
    return dag, by_name


async def test_a_dag_waiting_only_on_its_approval_is_parked(store):
    await _parked_dag(store)
    assert (await store.count_parked(), await store.count_active()) == (1, 0)


async def test_a_deferred_wave0_sibling_is_work(store):
    sibling = DAGNodeSpec(name="side", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(store, extra=[sibling])
    await store.update_node(by_name["side"].id, status="pending")  # deferred by a cap
    assert (await store.count_parked(), await store.count_active()) == (0, 1)


async def test_a_pending_node_behind_a_completed_predecessor_is_work(store):
    pre = DAGNodeSpec(name="pre", type=DAGNodeType.subtask, instructions="s")
    post = DAGNodeSpec(name="post", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(
        store, extra=[pre, post], edges=[DAGEdgeSpec(from_node="pre", to_node="post")]
    )
    await store.update_node(by_name["pre"].id, status="completed")
    await store.update_node(by_name["post"].id, status="pending")
    assert await store.count_active() == 1


async def test_a_pending_node_behind_the_waiting_approval_is_not_work(store):
    pre = DAGNodeSpec(name="pre", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(
        store, extra=[pre], edges=[DAGEdgeSpec(from_node="pre", to_node="send")]
    )
    await store.update_node(by_name["pre"].id, status="completed")
    assert await store.count_parked() == 1


async def test_a_running_sibling_is_work(store):
    sibling = DAGNodeSpec(name="side", type=DAGNodeType.subtask, instructions="s")
    _, by_name = await _parked_dag(store, extra=[sibling])
    await store.update_node(by_name["side"].id, status="running")
    assert await store.count_active() == 1


async def test_parked_dags_do_not_count_against_the_active_limit(store):
    for _ in range(5):
        await _parked_dag(store)
    # Five parked DAGs: an ordinary DAG is still admitted.
    await _one_node(store)


async def test_the_parked_cap_refuses_only_dags_with_an_approval(db):
    capped = DAGStore(
        db, f"test-p3cap-{uuid.uuid4().hex[:8]}",
        _settings(dag_max_parked_dags=1),
    )
    await _parked_dag(capped)
    with pytest.raises(ValueError, match="waiting on your answers"):
        await _parked_dag(capped)
    await _one_node(capped)  # no approval node: never refused by this cap


async def test_finalize_dag_refuses_while_a_node_is_open_or_the_dag_has_ended(store):
    dag, node = await _one_node(store)
    await store.update_dag_status(dag.id, "running")

    assert not await store.finalize_dag(dag.id, "failed", "stale")  # the node is still open
    assert (await store.get_dag(dag.id)).status == "running"

    await store.update_node(node.id, status="failed", error="boom")
    assert await store.finalize_dag(dag.id, "failed", "Failed nodes: n")
    ended = await store.get_dag(dag.id)
    assert (ended.status, ended.result_summary) == ("failed", "Failed nodes: n")
    assert ended.completed_at is not None

    assert not await store.finalize_dag(dag.id, "completed", "again")  # no longer live
    assert (await store.get_dag(dag.id)).status == "failed"
