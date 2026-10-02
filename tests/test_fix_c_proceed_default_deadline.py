"""Post-merge review P2-6: what a deadline may approve.

A failed push leaves the approval node parked with no linked card, retried
each tick until the deadline. Phase 3 could apply the default there
unconditionally because it was always a stop. With a proceed default (harness
Phase 2.8) that approved a question nobody saw, and it kept approving after
the flag that allows proceed defaults had been turned off.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from test_dag_approval import FakeSurfaceService, _approve, _node, _orch, _settings, _working_dag

from nous.dag.approval import refusal_message
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import MAX_ACTIVE_DAGS, DAGStore


@pytest.fixture(autouse=True)
def proceed_defaults_on(monkeypatch):
    """DAGCreateRequest builds a real Settings() to read the proceed-default
    flag, so it and its prerequisites come from the environment, as in prod."""
    for name in (
        "NOUS_DAG_APPROVAL_PROCEED_DEFAULT_ENABLED",
        "NOUS_COMPENSATION_ENABLED",
        "NOUS_COMPENSATION_AUTO_REVIEW_ENABLED",
        "NOUS_EXECUTION_LEDGER_PERSIST_ENABLED",
        "NOUS_A2UI_ENABLED",
    ):
        monkeypatch.setenv(name, "true")


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-fixc-card-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    mgr.get.return_value = None
    return mgr


@pytest.fixture
def surfaces():
    return FakeSurfaceService()


def _request(default_option: str) -> DAGCreateRequest:
    """draft -> approve -> send (undoable); "send" is the proceed option, "hold" the stop."""
    return DAGCreateRequest(
        name="mail",
        nodes=[
            DAGNodeSpec(name="draft", type=DAGNodeType.subtask, instructions="draft"),
            _approve(default_option=default_option),
            DAGNodeSpec(name="send", type=DAGNodeType.subtask, instructions="send", undoable=True),
        ],
        edges=[
            DAGEdgeSpec(from_node="draft", to_node="approve", edge_type="context_flow"),
            DAGEdgeSpec(from_node="approve", to_node="send", edge_type="context_flow"),
        ],
    )


async def _parked_past_its_deadline(store, orch, surfaces, *, delivered: bool, default_option: str = "send"):
    if not delivered:
        surfaces.push_errors = [RuntimeError("push down")]
    dag = await store.create(_request(default_option))
    await orch.start_dag(dag.id)
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="completed", result="Dear Bob, ...")
    await orch._advance_dag(await store.get_dag(dag.id))  # the approval parks and pushes its card
    node = await _node(store, dag.id, "approve")
    assert node.status == "awaiting_input"
    assert (node.surface_id is not None) is delivered
    await store.update_node(node.id, answer_deadline=datetime.now(UTC) - timedelta(seconds=1))
    return dag


async def test_an_undelivered_card_stops_instead_of_applying_a_proceed_default(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await _parked_past_its_deadline(store, orch, surfaces, delivered=False)

    await orch._advance_dag(await store.get_dag(dag.id))

    approve = await _node(store, dag.id, "approve")
    assert approve.status == "failed"
    assert approve.completed_at is not None
    assert (approve.answer, approve.answer_source) == (None, None)  # nobody answered and no default applied
    assert approve.error == "no approval card is linked; default 'Send it' (send) not applied"
    send = await _node(store, dag.id, "send")
    assert (send.status, send.subtask_id) == ("blocked", None)
    assert (await store.get_dag(dag.id)).status == "failed"


async def test_a_proceed_default_stops_once_the_flag_is_turned_off(store, subtask_mgr, surfaces):
    """The flag is a kill switch, not only a creation gate: a DAG parked while
    it was on does not proceed by default after a restart with it off."""
    dag = await _parked_past_its_deadline(store, _orch(store, subtask_mgr, surfaces), surfaces, delivered=True)
    restarted = _orch(store, subtask_mgr, surfaces, dag_approval_proceed_default_enabled=False)

    await restarted._advance_dag(await store.get_dag(dag.id))

    approve = await _node(store, dag.id, "approve")
    assert approve.status == "failed"
    assert (approve.answer, approve.answer_source) == (None, None)
    assert approve.error == "proceed defaults are turned off; default 'Send it' (send) not applied"
    assert surfaces.live() == {}  # the delivered card is closed
    send = await _node(store, dag.id, "send")
    assert (send.status, send.subtask_id) == ("blocked", None)
    assert (await store.get_dag(dag.id)).status == "failed"

    # The close is best-effort, so a tap can still reach the stopped node: it is told what happened.
    tap = await restarted.answer_node(approve.id, "send", source="companion", actor=None, surface_id=approve.surface_id)
    assert tap.outcome == "closed"
    assert refusal_message(tap, "send") == "this question was closed without an answer"


async def test_a_delivered_card_left_unanswered_proceeds_and_is_not_called_approved(store, subtask_mgr, surfaces):
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await _parked_past_its_deadline(store, orch, surfaces, delivered=True)

    await orch._advance_dag(await store.get_dag(dag.id))

    after = await store.get_dag(dag.id)
    by_name = {n.name: n for n in after.nodes}
    assert (by_name["approve"].status, by_name["approve"].answer, by_name["approve"].answer_source) == (
        "completed",
        "send",
        "deadline",
    )
    assert by_name["send"].status == "running"
    task = await orch._build_predecessor_context(by_name["send"], after)
    assert "default 'Send it' (send) applied" in task
    assert "[Approved input" not in task
    assert "[Input from 'draft' — nobody answered 'approve'; its default applied]: Dear Bob, ..." in task


async def test_a_dag_held_after_a_default_applied_is_not_called_approved(store, subtask_mgr, surfaces):
    """The dispatch gate's text, the label's sibling: "approved" is a person's answer."""
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await _parked_past_its_deadline(store, orch, surfaces, delivered=True)  # created first: the oldest DAG
    for _ in range(MAX_ACTIVE_DAGS):
        await _working_dag(store)

    await orch.tick()  # the default applies; every working slot is taken, so 'send' is held

    assert (await _node(store, dag.id, "approve")).answer_source == "deadline"
    assert (await _node(store, dag.id, "send")).status == "pending"
    assert orch.held_reason(dag.id) == f"waiting for a free slot ({MAX_ACTIVE_DAGS}/{MAX_ACTIVE_DAGS} DAGs working)"


async def test_a_tap_through_an_unlinked_card_beats_the_undelivered_stop(store, subtask_mgr, surfaces):
    """Guard for the race the stop relies on: its write is conditional, so an
    answer that landed after the tick loaded the node is never overwritten."""
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await _parked_past_its_deadline(store, orch, surfaces, delivered=False)
    stale = await store.get_dag(dag.id)  # the tick's copy: parked, unlinked, past its deadline
    node = next(n for n in stale.nodes if n.name == "approve")
    tap = await orch.answer_node(node.id, "send", source="companion", actor=None, surface_id="card-9")
    assert tap.outcome == "recorded"

    await orch._poll_awaiting_input(stale)

    approve = await _node(store, dag.id, "approve")
    assert (approve.status, approve.answer, approve.answer_source) == ("completed", "send", "companion")


@pytest.mark.parametrize("changed", ["dag_ended", "deadline_moved"])
async def test_a_stop_does_not_land_on_a_row_that_changed_under_the_tick(store, subtask_mgr, surfaces, changed):
    """Guard: the stop is written under the deadline answer's own conditions --
    the DAG still live and the deadline passed, both decided in SQL -- and a
    stop that did not land leaves the card alone. The second case needs another
    process: within one, a parked node's deadline is written only when it parks."""
    dag = await _parked_past_its_deadline(store, _orch(store, subtask_mgr, surfaces), surfaces, delivered=True)
    restarted = _orch(store, subtask_mgr, surfaces, dag_approval_proceed_default_enabled=False)
    stale = await store.get_dag(dag.id)  # the tick's copy: parked, past its deadline
    node = next(n for n in stale.nodes if n.name == "approve")
    if changed == "dag_ended":
        await store.update_dag_status(dag.id, "cancelled", result_summary="ended elsewhere")
    else:
        await store.update_node(node.id, answer_deadline=datetime.now(UTC) + timedelta(hours=1))

    await restarted._poll_awaiting_input(stale)

    approve = await _node(store, dag.id, "approve")
    assert (approve.status, approve.error) == ("awaiting_input", None)
    assert list(surfaces.live()) == [node.surface_id]


async def test_an_undelivered_card_still_applies_a_stop_default(store, subtask_mgr, surfaces):
    """Guard: Phase 3 behavior, unchanged -- a stop default needs no delivered card."""
    orch = _orch(store, subtask_mgr, surfaces)
    dag = await _parked_past_its_deadline(store, orch, surfaces, delivered=False, default_option="hold")

    await orch._advance_dag(await store.get_dag(dag.id))

    approve = await _node(store, dag.id, "approve")
    assert (approve.status, approve.answer, approve.answer_source) == ("failed", "hold", "deadline")
    assert approve.error.endswith("default 'Don't send' (hold) applied")
    assert (await _node(store, dag.id, "send")).status == "blocked"
