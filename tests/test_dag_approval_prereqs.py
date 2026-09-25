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


async def test_transition_node_applies_only_from_the_listed_statuses(store):
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")  # wave 0 → 'ready'

    assert await store.transition_node(draft.id, from_statuses={"pending"}, status="running") is False
    assert await store.transition_node(draft.id, from_statuses={"ready"}, status="running") is True
    assert (await _node(store, dag.id, "draft")).status == "running"


async def test_transition_node_honours_dag_statuses(store):
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")
    await store.update_dag_status(dag.id, "cancelled")

    assert (
        await store.transition_node(
            draft.id, from_statuses={"ready"}, dag_statuses={"pending", "running"}, status="running"
        )
        is False
    )


async def test_dispatch_does_not_resurrect_a_node_cancelled_after_the_load(store, subtask_mgr):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    stale = await store.get_dag(dag.id)  # the tick's copy: draft is 'ready'
    draft = next(n for n in stale.nodes if n.name == "draft")
    await store.update_node(draft.id, status="cancelled", error="cancelled")  # cancel_dag lands

    await orch._dispatch_ready_nodes(stale, [draft])

    assert (await _node(store, dag.id, "draft")).status == "cancelled"
    subtask_mgr.create.assert_not_called()


async def test_cancel_dag_keeps_an_outcome_that_landed_after_its_load(
    store, subtask_mgr, monkeypatch
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    real_cancel = orch._cancel_node

    async def completes_first(node):
        # The node finishes between cancel_dag's load and its write.
        await store.update_node(node.id, status="completed", result="done")
        await real_cancel(node)

    monkeypatch.setattr(orch, "_cancel_node", completes_first)

    await orch.cancel_dag(dag.id)

    assert (await _node(store, dag.id, "draft")).status == "completed"


async def test_retry_refuses_when_the_node_changed_after_its_load(
    store, subtask_mgr, monkeypatch
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="failed", error="boom")
    await store.update_dag_status(dag.id, "failed")

    async def another_retry_lands(node, _dag):
        await store.update_node(node.id, status="pending", error=None)
        return True

    monkeypatch.setattr(orch, "_account_before_retry", another_retry_lands)

    with pytest.raises(ValueError, match="changed state"):
        await orch.retry_node(dag.id, "draft")
    assert (await store.get_dag(dag.id)).status == "failed"  # not reactivated


async def test_a_cascade_target_that_finished_first_does_not_block_its_dependents(
    store, subtask_mgr, monkeypatch
):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(
        DAGCreateRequest(
            name="cascade",
            nodes=[
                DAGNodeSpec(name="src", type=DAGNodeType.subtask, instructions="s"),
                DAGNodeSpec(name="mid", type=DAGNodeType.subtask, instructions="m"),
                DAGNodeSpec(name="leaf", type=DAGNodeType.subtask, instructions="l"),
            ],
            edges=[
                DAGEdgeSpec(from_node="src", to_node="mid", edge_type="cancel_cascade"),
                DAGEdgeSpec(from_node="mid", to_node="leaf"),
            ],
        )
    )
    await store.update_dag_status(dag.id, "running")
    await store.update_node((await _node(store, dag.id, "src")).id, status="failed", error="boom")
    mid = await _node(store, dag.id, "mid")
    await store.update_node(mid.id, status="running", started_at=datetime.now(UTC))
    real_cancel = orch._cancel_node

    async def mid_finishes_first(node):
        if node.name == "mid":
            await store.update_node(node.id, status="completed", result="done")
        await real_cancel(node)

    monkeypatch.setattr(orch, "_cancel_node", mid_finishes_first)

    await orch._advance_dag(await store.get_dag(dag.id))

    assert (await _node(store, dag.id, "mid")).status == "completed"
    assert (await _node(store, dag.id, "leaf")).status == "pending"  # not blocked


async def test_a_deferral_does_not_resurrect_a_cancelled_node(store, subtask_mgr):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    stale = await store.get_dag(dag.id)
    draft = next(n for n in stale.nodes if n.name == "draft")  # 'ready' in the tick's copy
    await store.update_node(draft.id, status="cancelled", error="cancelled")

    await orch._defer_node(draft, stale, "pool saturated")

    assert (await _node(store, dag.id, "draft")).status == "cancelled"


async def test_a_cancel_during_subtask_creation_is_not_overwritten(store, subtask_mgr):
    """The launch's own `running` write came after the await on create(): a
    cancel_dag in that window saw no subtask_id to cancel, and the blind
    write resurrected the node — the subtask then ran in a cancelled DAG."""
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    draft = await _node(store, dag.id, "draft")
    created = SimpleNamespace(id=uuid.uuid4(), status="pending")

    async def cancel_lands_during_create(**_):
        await store.update_node(draft.id, status="cancelled", error="cancelled")
        return created

    subtask_mgr.create.side_effect = cancel_lands_during_create

    await orch.start_dag(dag.id)

    assert (await _node(store, dag.id, "draft")).status == "cancelled"
    subtask_mgr.cancel.assert_awaited_once_with(created.id)


def _raise_on_running_write(store, *, after_commit: bool = False):
    """Make the launch's `running` write raise (a transient DB error), before
    or after it lands; every other transition goes through untouched."""
    real = store.transition_node

    async def flaky(node_id, **kwargs):
        if kwargs.get("status") == "running":
            if after_commit:
                await real(node_id, **kwargs)
            raise RuntimeError("connection reset")
        return await real(node_id, **kwargs)

    store.transition_node = flaky


async def test_a_raising_launch_write_cancels_the_subtask_it_created(store, subtask_mgr):
    """The node reads failed, so the work it launched must not run on."""
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    _raise_on_running_write(store)

    await orch.start_dag(dag.id)

    assert (await _node(store, dag.id, "draft")).status == "failed"
    subtask_mgr.cancel.assert_awaited_once_with(subtask_mgr.create.return_value.id)


async def test_a_launch_write_that_landed_before_raising_keeps_its_subtask(store, subtask_mgr):
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    _raise_on_running_write(store, after_commit=True)

    await orch.start_dag(dag.id)

    draft = await _node(store, dag.id, "draft")
    assert draft.status == "running"
    assert draft.subtask_id == subtask_mgr.create.return_value.id
    subtask_mgr.cancel.assert_not_awaited()


async def test_a_raising_launch_write_disables_the_check_it_created(store, subtask_mgr):
    """A leaked check is urgent and exempt from quiet hours; record its name so
    the reconciliation sweep can retry the disable."""
    orch = _orch(store, subtask_mgr)
    dag = await store.create(
        DAGCreateRequest(
            name="p3-check",
            nodes=[DAGNodeSpec(name="watch", type=DAGNodeType.check, instructions="watch it")],
        )
    )
    _raise_on_running_write(store)

    await orch.start_dag(dag.id)

    node = await _node(store, dag.id, "watch")
    assert node.status == "failed"
    assert node.check_name == f"dag-{dag.id.hex[:8]}-watch"
    orch._dynamic_loader.manage_check.assert_awaited_once_with(
        action="disable", name=node.check_name
    )


async def test_the_subtask_is_abandoned_even_when_the_failure_write_raises_too(store, subtask_mgr):
    """One DB fault usually fails both writes; the abandon must not sit behind
    the second one, or the node is relaunched while the first run goes on."""
    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    real = store.transition_node

    async def down(node_id, **kwargs):
        if kwargs.get("status") in ("running", "failed"):
            raise ConnectionError("connection reset")
        return await real(node_id, **kwargs)

    store.transition_node = down

    await orch.start_dag(dag.id)

    subtask_mgr.cancel.assert_awaited_once_with(subtask_mgr.create.return_value.id)


def _running(**kw) -> SimpleNamespace:
    base = dict(status="running", tokens_in=0, tokens_out=0, result=None, error=None)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_a_retry_that_lands_mid_tick_is_not_finalized_over(store, subtask_mgr):
    """The completion check reads the tick's snapshot. A retry that landed
    after the load must win, or the DAG ends 'failed' with pending nodes and
    no retry or cancel can move it again."""
    orch = _orch(store, subtask_mgr)
    dag = await store.create(
        DAGCreateRequest(
            name="p3-par",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask, instructions="a"),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask, instructions="b"),
                DAGNodeSpec(name="other", type=DAGNodeType.subtask, instructions="other"),
            ],
            edges=[DAGEdgeSpec(from_node="a", to_node="b")],
        )
    )
    await orch.start_dag(dag.id)
    await store.update_node((await _node(store, dag.id, "a")).id, status="failed", error="boom")
    subtask_mgr.get.return_value = _running()
    await orch.tick()  # b blocked; 'other' still running
    fired: list[int] = []

    async def other_finishes(_subtask_id):
        if not fired:
            fired.append(1)
            await orch.retry_node(dag.id, "a")  # lands while the tick syncs 'other'
        return _running(status="completed", result="ok")

    subtask_mgr.get.side_effect = other_finishes
    await orch.tick()

    after = await store.get_dag(dag.id)
    assert after.status == "running"
    assert {n.name: n.status for n in after.nodes}["a"] == "pending"


async def test_stale_ready_recovery_does_not_resurrect_a_cancelled_node(db, store, subtask_mgr):
    from datetime import timedelta

    from sqlalchemy import update as sa_update

    from nous.storage.models import ExecutionDAG

    orch = _orch(store, subtask_mgr)
    dag = await store.create(_two_node("dependency"))
    await store.update_dag_status(dag.id, "running")  # start_dag bypassed: an orphan ready wave-0
    async with db.session() as session:
        await session.execute(
            sa_update(ExecutionDAG)
            .where(ExecutionDAG.id == dag.id)
            .values(started_at=datetime.now(UTC) - timedelta(seconds=400))
        )
        await session.commit()
    stale = await store.get_dag(dag.id)
    draft = await _node(store, dag.id, "draft")
    await store.update_node(draft.id, status="cancelled", error="cancelled")  # cancel_dag, after the load

    await orch._recover_stale_ready_nodes(stale)

    assert (await _node(store, dag.id, "draft")).status == "cancelled"
