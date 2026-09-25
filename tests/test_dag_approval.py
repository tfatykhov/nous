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
from sqlalchemy import update as sa_update

from nous.config import Settings
from nous.dag.approval import approval_dedup_key
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import MAX_ACTIVE_DAGS, DAGStore
from nous.storage.models import ExecutionDAG


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


async def test_a_tap_that_proceeds_is_recorded_once(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    first = await orch.answer_node(
        node.id, "send", source="companion", actor="unattributed", surface_id=node.surface_id
    )
    second = await orch.answer_node(
        node.id, "hold", source="companion", actor="unattributed", surface_id=node.surface_id
    )

    assert first.outcome == "recorded"
    assert (second.outcome, second.option_label) == ("closed", "Send it")
    node = await _node(store, dag.id, "approve")
    assert (node.status, node.answer, node.answer_source, node.answered_by) == (
        "completed", "send", "companion", "unattributed",
    )
    assert node.result.startswith("Answered in the companion: 'Send it' (send)")
    assert " by " not in node.result


async def test_an_attributed_actor_is_named(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    _, node = await _parked(store, orch)
    await orch.answer_node(node.id, "send", source="companion", actor="alice@example.com", surface_id=node.surface_id)
    assert (await _node(store, node.dag_id, "approve")).result.endswith("by alice@example.com")


async def test_a_stop_answer_blocks_the_successor_and_stops_the_dag(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    result = await orch.answer_node(
        node.id, "hold", source="companion", actor="unattributed", surface_id=node.surface_id
    )
    await orch._advance_dag(await store.get_dag(dag.id))

    assert result.outcome == "recorded"
    assert (await _node(store, dag.id, "approve")).error.startswith("declined in the companion")
    assert (await _node(store, dag.id, "send")).status == "blocked"
    assert (await store.get_dag(dag.id)).status == "failed"


async def test_a_tap_before_the_link_is_recorded_and_links(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, node = await _parked(store, orch)

    result = await orch.answer_node(node.id, "send", source="companion", actor="unattributed", surface_id="card-7")

    assert result.outcome == "recorded"
    assert (await _node(store, dag.id, "approve")).surface_id == "card-7"


@pytest.mark.parametrize(
    "setup, expected",
    [("stray", "stray_card"), ("dag_ended", "dag_ended"), ("pending", "not_open")],
)
async def test_refused_taps_say_why(store, subtask_mgr, surfaces, setup, expected):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    card = node.surface_id
    if setup == "stray":
        card = "some-other-card"
    elif setup == "dag_ended":
        await store.update_dag_status(dag.id, "cancelled")
    else:
        await store.update_node(node.id, status="pending")

    result = await orch.answer_node(node.id, "send", source="companion", actor="unattributed", surface_id=card)

    assert result.outcome == expected


async def test_invalid_option_and_unknown_node(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    _, node = await _parked(store, orch)
    assert (await orch.answer_node(node.id, "maybe", source="companion", actor=None)).outcome == "invalid_option"
    assert (await orch.answer_node(uuid.uuid4(), "send", source="companion", actor=None)).outcome == "not_linked"


async def test_the_deadline_applies_the_stop_default_in_one_tick(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    await store.update_node(node.id, answer_deadline=datetime.now(UTC) - timedelta(seconds=1))

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert (node.status, node.answer, node.answer_source) == ("failed", "hold", "deadline")
    assert node.error.endswith("default 'Don't send' (hold) applied")
    assert surfaces.live() == {}
    assert (await _node(store, dag.id, "send")).status == "blocked"
    assert (await store.get_dag(dag.id)).status == "failed"


async def test_nothing_happens_before_the_deadline(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    await orch._advance_dag(await store.get_dag(dag.id))
    assert (await _node(store, dag.id, "approve")).status == "awaiting_input"


async def test_a_lost_card_is_pushed_again_and_relinked(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    surfaces.cards[node.surface_id]["status"] = "expired"  # closed behind the node's back

    await orch._advance_dag(await store.get_dag(dag.id))

    relinked = await _node(store, dag.id, "approve")
    assert relinked.surface_id != node.surface_id
    assert relinked.surface_id in surfaces.live()
    assert len(surfaces.pings) == 2


async def test_a_transient_push_failure_heals_on_the_next_tick(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, _ = await _parked(store, orch)

    await orch._advance_dag(await store.get_dag(dag.id))

    node = await _node(store, dag.id, "approve")
    assert node.surface_id in surfaces.live() and node.error is None


async def test_a_crash_between_push_and_link_adopts_the_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    card = node.surface_id
    await store.update_node(node.id, surface_id=None)  # the link write never landed

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "approve")).surface_id == card
    assert len(surfaces.cards) == 1 and len(surfaces.pings) == 1  # adopted, not re-pushed


async def test_the_acting_node_receives_the_approved_draft(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request(with_draft=True))
    await orch.start_dag(dag.id)
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="completed", result="Dear Bob, ...")
    await orch._advance_dag(await store.get_dag(dag.id))  # the approval parks
    node = await _node(store, dag.id, "approve")
    await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id=node.surface_id)
    subtask_mgr.create.reset_mock()

    await orch._advance_dag(await store.get_dag(dag.id))  # 'send' launches

    task = subtask_mgr.create.call_args.kwargs["task"]
    assert "[Approved input from 'draft' (approved at 'approve')]: Dear Bob, ..." in task
    assert "Answered in the companion: 'Send it' (send)" in task


async def test_no_repush_for_a_node_a_tap_already_answered(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, node = await _parked(store, orch)
    stale = await store.get_dag(dag.id)  # the tick's copy: parked, unlinked
    await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id="card-9")

    await orch._poll_awaiting_input(stale)

    assert surfaces.cards == {}


async def test_cancel_dag_cancels_a_waiting_node_and_closes_its_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)

    await orch.cancel_dag(dag.id)
    late = await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id=node.surface_id)

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}
    assert (late.outcome, late.node_status) == ("closed", "cancelled")


async def test_cancel_dag_loses_to_an_answer_that_landed_first(store, subtask_mgr, surfaces, monkeypatch):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    real_cancel = orch._cancel_node

    async def answered_first(n):
        if n.id == node.id:
            await orch.answer_node(n.id, "send", source="companion", actor=None, surface_id=node.surface_id)
        await real_cancel(n)

    monkeypatch.setattr(orch, "_cancel_node", answered_first)
    await orch.cancel_dag(dag.id)

    assert (await _node(store, dag.id, "approve")).status == "completed"


async def test_a_cascade_cancel_closes_the_card(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    request = _request()
    request.nodes.append(DAGNodeSpec(name="src", type=DAGNodeType.subtask, instructions="s"))
    request.edges.append(DAGEdgeSpec(from_node="src", to_node="approve", edge_type="cancel_cascade"))
    dag = await store.create(DAGCreateRequest(name="c", nodes=request.nodes, edges=request.edges))
    await orch.start_dag(dag.id)
    src = await _node(store, dag.id, "src")
    await store.update_node(src.id, status="failed", error="boom")

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}


async def test_the_budget_path_cancels_a_waiting_node(db, store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces, dag_token_budget_enforcement_enabled=True)
    dag = await store.create(
        DAGCreateRequest(name="b", nodes=_request().nodes, edges=_request().edges, token_budget=10)
    )
    await orch.start_dag(dag.id)
    async with db.session() as session:
        await session.execute(
            sa_update(ExecutionDAG).where(ExecutionDAG.id == dag.id).values(tokens_consumed=20)
        )
        await session.commit()

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}
    assert (await store.get_dag(dag.id)).status in ("failed", "partial")


async def test_the_sweep_closes_leaked_cards_and_leaves_fresh_ones(store, subtask_mgr, surfaces):
    """Two live cards under one dedup key cannot happen on Postgres (the
    partial UNIQUE index on (agent_id, dedup_key) WHERE live) — the stray
    rule is defensive, and the fake lets us exercise it."""
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    key = approval_dedup_key(node.id)
    surfaces.cards["stray"] = {"dedup_key": key, "status": "live", "built": None}
    surfaces.cards["ghost"] = {"dedup_key": approval_dedup_key(uuid.uuid4()), "status": "live", "built": None}

    await orch._sweep_leaked_approval_cards()

    assert set(surfaces.live()) == {node.surface_id}  # stray + ghost closed, the linked card kept


async def test_the_sweep_leaves_an_unlinked_fresh_card_alone(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(_request())
    node_id = (await _node(store, dag.id, "approve")).id

    async def sweep_between_push_and_link(_sid):
        await orch._sweep_leaked_approval_cards()

    surfaces.on_push = sweep_between_push_and_link
    await orch.start_dag(dag.id)

    node = await _node(store, dag.id, "approve")
    assert node.id == node_id and node.surface_id in surfaces.live()


async def test_the_sweep_cancels_a_waiting_node_in_a_finished_dag(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag, node = await _parked(store, orch)
    await store.update_dag_status(dag.id, "failed")

    await orch._sweep_leaked_approval_cards()

    assert (await _node(store, dag.id, "approve")).status == "cancelled"
    assert surfaces.live() == {}


async def test_the_sweep_finds_a_stranded_node_that_has_no_card(store, subtask_mgr, surfaces):
    """The probe's end state: awaiting_input in a cancelled DAG, no card.
    The card-driven pass cannot see it; the node-driven query must."""
    orch = _orch(store, subtask_mgr, surfaces)
    surfaces.push_errors = [RuntimeError("flaky")]
    dag, _ = await _parked(store, orch)  # parked, push failed: no card
    await store.update_dag_status(dag.id, "cancelled")

    await orch._sweep_leaked_approval_cards()

    assert (await _node(store, dag.id, "approve")).status == "cancelled"


async def _working_dag(store):
    dag = await store.create(
        DAGCreateRequest(name="w", nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")])
    )
    await store.update_dag_status(dag.id, "running")
    await store.update_node(dag.nodes[0].id, status="running", started_at=datetime.now(UTC))
    return dag


async def test_a_resumed_dag_waits_for_a_working_slot(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    resumed, node = await _parked(store, orch)  # created first: the oldest DAG
    others = [await _working_dag(store) for _ in range(MAX_ACTIVE_DAGS)]
    await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id=node.surface_id)
    subtask_mgr.create.reset_mock()

    await orch.tick()

    assert (await _node(store, resumed.id, "send")).status == "pending"
    subtask_mgr.create.assert_not_called()
    assert orch._defer_counts.get((await _node(store, resumed.id, "send")).id) is None

    assert orch.held_reason(resumed.id) == (
        f"approved — waiting for a free slot ({MAX_ACTIVE_DAGS}/{MAX_ACTIVE_DAGS} DAGs working)"
    )

    await store.update_node(others[0].nodes[0].id, status="completed")
    await orch.tick()

    assert (await _node(store, resumed.id, "send")).status == "running"
    assert orch.held_reason(resumed.id) is None


async def test_a_held_dag_still_asks_its_next_question(store, subtask_mgr, surfaces):
    """Parking an approval takes no subtask; holding it would only delay the question."""
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await store.create(
        DAGCreateRequest(
            name="two-step",
            nodes=[
                _approve(),
                _approve(name="approve2", instructions="And the follow-up?"),
                DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send"),
            ],
            edges=[
                DAGEdgeSpec(from_node="approve", to_node="approve2", edge_type="context_flow"),
                DAGEdgeSpec(from_node="approve2", to_node="send", edge_type="context_flow"),
            ],
        )
    )
    await orch.start_dag(dag.id)
    for _ in range(MAX_ACTIVE_DAGS):
        await _working_dag(store)
    first = await _node(store, dag.id, "approve")
    await orch.answer_node(first.id, "send", source="companion", actor=None, surface_id=first.surface_id)

    await orch.tick()

    assert (await _node(store, dag.id, "approve2")).status == "awaiting_input"


async def test_a_dag_without_an_approval_is_never_held(store, subtask_mgr, surfaces):
    """Even while the gate runs (an approval DAG exists) and every slot is
    taken, a plain DAG between waves dispatches as it always has."""
    orch = _orch(store, subtask_mgr, surfaces)
    await _parked(store, orch)  # an approval DAG exists, so the gate runs
    late = await _working_dag(store)
    await store.update_dag_status(late.id, "completed")  # outside admission's count for now
    plain = await store.create(
        DAGCreateRequest(
            name="plain",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.gate),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="b"),
            ],
            edges=[DAGEdgeSpec(from_node="a", to_node="b")],
        )
    )
    for _ in range(MAX_ACTIVE_DAGS - 1):
        await _working_dag(store)
    await store.update_dag_status(late.id, "running")  # a retry reactivates it past the limit
    await store.update_dag_status(plain.id, "running")
    await store.update_node(next(n for n in plain.nodes if n.name == "a").id, status="completed")

    await orch.tick()  # MAX_ACTIVE_DAGS DAGs are working; 'plain' is between waves

    assert (await _node(store, plain.id, "b")).status == "running"


def test_a_dag_polling_a_check_is_not_working():
    from nous.dag.orchestrator import _is_working

    assert not _is_working(SimpleNamespace(nodes=[SimpleNamespace(status="awaiting_check")]))
    assert _is_working(SimpleNamespace(nodes=[SimpleNamespace(status="running")]))


async def test_the_gate_is_inert_without_approval_nodes(store, subtask_mgr, surfaces, monkeypatch):
    orch = _orch(store, subtask_mgr, surfaces)
    for _ in range(3):
        await _working_dag(store)
    calls: list[bool] = []

    async def record(dag, *, may_start=True):
        calls.append(may_start)

    monkeypatch.setattr(orch, "_advance_dag", record)
    await orch.tick()

    assert calls == [True, True, True]
