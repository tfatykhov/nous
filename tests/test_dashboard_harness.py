"""Harness dashboard visibility (spec 2026-09-25 v2): the read-only data the
DAG, Ledger, Harness and Overview views render."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest_asyncio

from nous.api.dashboard_queries import get_dag_dashboard_data
from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore

BASE = "https://nous.example"
OPTIONS = [
    {"id": "send", "label": "Send it", "outcome": "proceed"},
    {"id": "hold", "label": "Don't send", "outcome": "stop"},
]


def _settings() -> Settings:
    return Settings(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)


@pytest_asyncio.fixture
async def agent_id() -> str:
    return f"test-hdash-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def store(db, agent_id):
    return DAGStore(db, agent_id, _settings())


async def _approval_dag(store, name="mail"):
    dag = await store.create(
        DAGCreateRequest(
            name=name,
            nodes=[
                DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="draft"),
                DAGNodeSpec(
                    name="approve", type=DAGNodeType.approval, instructions="Send the report?",
                    options=OPTIONS, default_option="hold",
                ),
                DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send"),
            ],
            edges=[
                DAGEdgeSpec(from_node="draft", to_node="approve", edge_type="context_flow"),
                DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"),
            ],
        )
    )
    await store.update_dag_status(dag.id, "running")
    return dag, {n.name: n for n in dag.nodes}


async def _dash(db, agent_id, **kw):
    async with db.session() as session:
        return await get_dag_dashboard_data(session, agent_id, **kw)


def _node(data, dag_id, name):
    dag = next(d for d in data["active_dags"] if UUID(d["id"]) == dag_id)
    return next(n for n in dag["nodes"] if n["name"] == name)


async def test_an_open_question_is_waiting_on_you_with_what_its_card_shows(db, store, agent_id):
    dag, n = await _approval_dag(store)
    deadline = datetime.now(UTC) + timedelta(hours=3)
    await store.update_node(n["draft"].id, status="completed", result="DRAFT BODY")
    await store.update_node(
        n["approve"].id, status="awaiting_input", surface_id="card-1",
        started_at=datetime.now(UTC), answer_deadline=deadline,
        answer_history=[{"answer": "hold", "label": "Don't send", "outcome": "stop",
                         "answer_source": "companion", "answered_by": "unattributed",
                         "answered_at": "2026-09-24T09:12:00+00:00"}],
    )

    data = await _dash(db, agent_id, public_base_url=BASE)

    view = _node(data, dag.id, "approve")["approval"]
    assert view["question"] == "Send the report?"
    assert view["default_label"] == "Don't send"
    assert view["card_url"] == f"{BASE}/companion#/s/card-1"
    assert view["card_error"] is None
    assert view["card_summary"].startswith("Send the report?")
    assert "From 'draft':\nDRAFT BODY" in view["card_summary"]
    assert view["reviewing"] == ["draft"]
    assert view["attempts"][0]["answered_by"] is None  # never "unattributed"
    assert view["answer"] is None
    assert data["stats"]["waiting_count"] == 1
    assert [w["node_name"] for w in data["waiting_on_you"]] == ["approve"]
    assert data["waiting_on_you"][0]["card_url"] == f"{BASE}/companion#/s/card-1"
    active = next(d for d in data["active_dags"] if UUID(d["id"]) == dag.id)
    assert active["waiting"] == 1
    assert "approval" not in _node(data, dag.id, "send")


async def test_an_undelivered_card_says_why(db, store, agent_id):
    dag, n = await _approval_dag(store)
    await store.update_node(
        n["approve"].id, status="awaiting_input", surface_id=None,
        error="approval card not delivered yet: companion down",
        answer_deadline=datetime.now(UTC) + timedelta(hours=1),
    )

    data = await _dash(db, agent_id, public_base_url=BASE)

    waiting = data["waiting_on_you"][0]
    assert waiting["card_url"] is None
    assert waiting["card_error"] == "approval card not delivered yet: companion down"


async def test_waiting_on_you_is_ordered_by_deadline_and_skips_answered(db, store, agent_id):
    late, ln = await _approval_dag(store, "late")
    soon, sn = await _approval_dag(store, "soon")
    done, dn = await _approval_dag(store, "done")
    now = datetime.now(UTC)
    await store.update_node(ln["approve"].id, status="awaiting_input", answer_deadline=now + timedelta(hours=9))
    await store.update_node(sn["approve"].id, status="awaiting_input", answer_deadline=now + timedelta(hours=1))
    await store.update_node(dn["approve"].id, status="completed", answer="send",
                            answer_source="deadline", answered_by="system:deadline")

    data = await _dash(db, agent_id)

    assert [w["dag_name"] for w in data["waiting_on_you"]] == ["soon", "late"]
    answered = _node(data, done.id, "approve")["approval"]
    assert answered["answer_label"] == "Send it"
    assert answered["answered_by"] is None  # the deadline actor is not a person


async def test_the_held_reason_comes_from_the_orchestrator_by_uuid(db, store, agent_id):
    dag, _ = await _approval_dag(store)
    seen = []

    def held(dag_id):
        seen.append(dag_id)
        return "approved — waiting for a free slot (5/5 DAGs working)" if dag_id == dag.id else None

    data = await _dash(db, agent_id, held_reason=held)

    active = next(d for d in data["active_dags"] if UUID(d["id"]) == dag.id)
    assert active["held_reason"] == "approved — waiting for a free slot (5/5 DAGs working)"
    assert all(isinstance(x, UUID) for x in seen)


async def test_a_held_reason_that_raises_reads_as_none(db, store, agent_id):
    """main.py passes a lazy proxy that raises RuntimeError when DAGs are off."""
    await _approval_dag(store)

    def broken(_dag_id):
        raise RuntimeError("component not initialised")

    data = await _dash(db, agent_id, held_reason=broken)

    assert all(d["held_reason"] is None for d in data["active_dags"])


async def _stopped(store, name, *, dag_status="failed", answer_source="companion"):
    dag, n = await _approval_dag(store, name)
    await store.update_node(n["draft"].id, status="completed", result="x")
    await store.update_node(n["approve"].id, status="failed", answer="hold", answer_source=answer_source,
                            error="declined")
    await store.update_node(n["send"].id, status="blocked")
    await store.update_dag_status(dag.id, dag_status, result_summary="Stopped at approval")
    return dag


async def test_stopped_by_says_who_stopped_it_and_only_for_failed_dags(db, store, agent_id):
    by_companion = await _stopped(store, "c")
    by_deadline = await _stopped(store, "d", answer_source="deadline")
    cancelled = await _stopped(store, "x", dag_status="cancelled")
    crashed, cn = await _approval_dag(store, "crash")
    await store.update_node(cn["draft"].id, status="failed", error="boom")
    await store.update_dag_status(crashed.id, "failed", result_summary="Failed nodes: draft")

    data = await _dash(db, agent_id)

    stopped = {UUID(d["id"]): d["stopped_by"] for d in data["recent_dags"]}
    assert stopped[by_companion.id] == "companion"
    assert stopped[by_deadline.id] == "deadline"
    assert stopped[cancelled.id] is None
    assert stopped[crashed.id] is None
