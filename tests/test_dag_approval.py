"""Harness Phase 3: the approval node in the orchestrator (spec §3.4-§3.11).

A FakeSurfaceService stands in for SurfaceService (whose own DB tests are
postgres_only because a2ui_surfaces.allowed_actions does not round-trip on
SQLite). It models what the orchestrator relies on: dedup replaces a LIVE
card in place (same id) and pings only when it creates one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.approval import approval_dedup_key
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


class FakeSurfaceService:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.pings: list[str | None] = []
        self.push_errors: list[BaseException] = []
        self.close_errors: list[BaseException] = []
        self.on_push = None  # async callable(surface_id) run before push returns
        self._seq = 0

    async def push_built(self, built, *, dedup_key=None, notify=None, notify_text=None,
                         reserved_key_ok=False, **_):
        if self.push_errors:
            raise self.push_errors.pop(0)
        assert reserved_key_ok, "the orchestrator must pass reserved_key_ok"
        live = [s for s, c in self.cards.items() if c["dedup_key"] == dedup_key and c["status"] == "live"]
        if live:
            sid = live[0]
            self.cards[sid]["built"] = built
        else:
            self._seq += 1
            sid = f"card-{self._seq}"
            self.cards[sid] = {"dedup_key": dedup_key, "status": "live", "built": built}
            self.pings.append(notify_text)
        if self.on_push is not None:
            await self.on_push(sid)
        return sid

    async def close(self, surface_id, status="expired"):
        if self.close_errors:
            raise self.close_errors.pop(0)
        card = self.cards.get(surface_id)
        if card and card["status"] == "live":
            card["status"] = status

    async def close_by_dedup_key(self, dedup_key, status="expired"):
        if self.close_errors:
            raise self.close_errors.pop(0)
        hit = [s for s, c in self.cards.items() if c["dedup_key"] == dedup_key and c["status"] == "live"]
        for sid in hit:
            self.cards[sid]["status"] = status
        return hit

    async def live_ids(self, surface_ids):
        return {s for s in surface_ids if self.cards.get(s, {}).get("status") == "live"}

    async def live_cards_by_prefix(self, prefix):
        return [
            (s, c["dedup_key"]) for s, c in self.cards.items()
            if c["status"] == "live" and (c["dedup_key"] or "").startswith(prefix)
        ]

    def live(self) -> dict[str, dict]:
        return {s: c for s, c in self.cards.items() if c["status"] == "live"}


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, dag_node_default_timeout=120, dag_node_max_timeout=3600)
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-p3orch-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    mgr.get.return_value = None
    return mgr


@pytest.fixture
def surfaces():
    return FakeSurfaceService()


def _orch(store, subtask_mgr, surfaces, **settings) -> DAGOrchestrator:
    orch = DAGOrchestrator(
        store=store, subtask_mgr=subtask_mgr, dynamic_loader=AsyncMock(),
        settings=_settings(**settings), surface_service=surfaces,
    )
    orch.clock_wired = True
    return orch


def _approve(**overrides) -> DAGNodeSpec:
    base = dict(
        name="approve", type=DAGNodeType.approval, instructions="Send the drafted email?",
        options=[
            {"id": "send", "label": "Send it", "outcome": "proceed"},
            {"id": "hold", "label": "Don't send", "outcome": "stop"},
        ],
        default_option="hold",
    )
    base.update(overrides)
    return DAGNodeSpec(**base)


def _request(with_draft: bool = False) -> DAGCreateRequest:
    nodes = [_approve(), DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send")]
    edges = [DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow")]
    if with_draft:
        nodes.insert(0, DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="draft"))
        edges.append(DAGEdgeSpec(from_node="draft", to_node="approve", edge_type="context_flow"))
    return DAGCreateRequest(name="mail", nodes=nodes, edges=edges)


async def _node(store, dag_id, name):
    dag = await store.get_dag(dag_id)
    return next(n for n in dag.nodes if n.name == name)


async def _parked(store, orch):
    dag = await store.create(_request())
    await orch.start_dag(dag.id)
    return dag, await _node(store, dag.id, "approve")


async def test_launch_parks_pushes_one_card_and_links_it(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    assert node.status == "awaiting_input"
    assert node.answer_deadline is not None
    assert node.surface_id in surfaces.live()
    card = surfaces.cards[node.surface_id]
    assert card["dedup_key"] == approval_dedup_key(node.id)
    data = card["built"].data_model
    assert data["summary"].startswith("Send the drafted email?")
    assert data["risk"].endswith("'Don't send' applies.")
    assert [o["label"] for o in data["options"]] == ["Send it — continues", "Don't send — stops here"]
    assert data["recommendation"] == ""
    assert len(surfaces.pings) == 1 and "No answer by" in surfaces.pings[0]
    # I3: the card outlives the node's deadline — the card never decides.
    deadline = node.answer_deadline
    deadline = deadline.replace(tzinfo=UTC) if deadline.tzinfo is None else deadline
    assert datetime.now(UTC) + card["built"].expires_in > deadline


async def test_the_card_carries_the_draft(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request(with_draft=True))
    await orch.start_dag(dag.id)
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="completed", result="Dear Bob, ...")

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert "From 'draft':\nDear Bob, ..." in surfaces.cards[node.surface_id]["built"].data_model["summary"]


async def test_step0_retires_the_previous_attempts_live_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    node = await _node(store, dag.id, "approve")
    surfaces.cards["old"] = {"dedup_key": approval_dedup_key(node.id), "status": "live", "built": None}

    await orch.start_dag(dag.id)

    node = await _node(store, dag.id, "approve")
    assert surfaces.cards["old"]["status"] == "expired"
    assert node.surface_id != "old" and node.surface_id in surfaces.live()


async def test_a_failing_step0_defers_instead_of_parking(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.close_errors = [RuntimeError("db down")]
    dag = await store.create(_request())

    await orch.start_dag(dag.id)

    node = await _node(store, dag.id, "approve")
    assert node.status == "pending"
    assert surfaces.cards == {}
    assert orch._defer_counts[node.id] == 1


async def test_a_transient_push_failure_leaves_the_node_parked(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    _, node = await _parked(store, orch)

    assert node.status == "awaiting_input"
    assert node.surface_id is None
    assert node.error.startswith("approval card not delivered yet")


async def test_a_censor_refusal_fails_the_node(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [PermissionError("surface blocked by censor: pii")]
    _, node = await _parked(store, orch)

    assert node.status == "failed"
    assert node.error.startswith("approval card refused by censor")


async def test_a_node_cancelled_before_the_link_gets_its_card_closed(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    node_id = (await _node(store, dag.id, "approve")).id

    async def cancel_lands(_sid):
        await store.update_node(node_id, status="cancelled", error="cancelled")

    surfaces.on_push = cancel_lands
    await orch.start_dag(dag.id)

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}


async def test_the_park_write_is_the_reset_point(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    await store.update_dag_status(dag.id, "running")
    node = await _node(store, dag.id, "approve")
    at = datetime.now(UTC) - timedelta(days=1)
    await store.update_node(
        node.id, status="pending", answer="hold", answer_source="deadline",
        answered_by="system:deadline", answered_at=at, error="no answer by …",
    )

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert node.status == "awaiting_input"
    assert (node.answer, node.answer_source, node.answered_at, node.error) == (None, None, None, None)
    assert [h["answer"] for h in node.answer_history] == ["hold"]


def test_approvals_wired(store, subtask_mgr, surfaces):
    assert _orch(store, subtask_mgr, surfaces).approvals_wired
    assert not _orch(store, subtask_mgr, None).approvals_wired
