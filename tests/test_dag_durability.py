"""Tests for F087 — DAG durability backstops.

Three properties, each closing a gap found at HEAD:

* **Reaper.** `_effective_timeout` was consumed only at launch and inside
  `_poll_awaiting_checks`; nothing checked elapsed time for a node in
  status='running'. An orphaned subtask therefore left the node running
  forever, which kept its DAG running forever, which consumed one of the
  five MAX_ACTIVE_DAGS slots forever.
* **Token accounting.** `DAGStore.update_dag_tokens` had no production
  caller, so `tokens_consumed` was structurally 0 and the budget branch in
  `_advance_dag` was unreachable.
* **Fail-loud wiring.** `register_dag_tools` ran unconditionally while the
  tick was wired only `if heartbeat_runner is not None`, so with the
  heartbeat off the agent created DAGs that never advanced, silently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.api.tools import ToolDispatcher, register_dag_tools
from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGEdgeSpec, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


def _settings(**overrides) -> Settings:
    """Hermetic settings — never inherit the developer's .env."""
    base = dict(
        _env_file=None,
        dag_node_default_timeout=120,
        dag_node_max_timeout=3600,
        dag_node_timeout_grace_seconds=300,
    )
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-durability-{uuid.uuid4().hex[:8]}", _settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    return mgr


@pytest.fixture
def dynamic_loader():
    loader = AsyncMock()
    loader.create_check = AsyncMock(return_value={"name": "test-check"})
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


def _orch(store, subtask_mgr, dynamic_loader, settings=None):
    orch = DAGOrchestrator(
        store=store,
        subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader,
        settings=settings or _settings(),
    )
    orch.clock_wired = True
    return orch


def _subtask_dag(name: str = "reaper-dag") -> DAGCreateRequest:
    return DAGCreateRequest(
        name=name,
        nodes=[
            DAGNodeSpec(
                name="long-runner",
                type=DAGNodeType.subtask,
                instructions="Run a long job",
                timeout_seconds=120,
            ),
        ],
    )


def _cancel_takes_effect(mgr, subtask_id, exec_started=None):
    """Wire subtask_mgr so cancellation actually flips the row terminal.

    Production `SubtaskManager.cancel()` moves a running row to 'cancelled',
    so the reaper's post-cancel confirmation read sees 'cancelled'. A mock
    whose status never changes is describing a world where cancel silently
    failed — which the reaper now (correctly) refuses to treat as a clean
    reap, so these tests must model the real transition.
    """
    state = {"status": "running"}

    async def _get(_sid):
        return SimpleNamespace(
            id=subtask_id, status=state["status"], result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
            started_at=exec_started,
        )

    async def _cancel(_sid):
        state["status"] = "cancelled"
        return True

    mgr.get = AsyncMock(side_effect=_get)
    mgr.cancel = AsyncMock(side_effect=_cancel)
    return mgr


async def _running_node_started_at(store: DAGStore, dag, when: datetime):
    """Put the DAG's single node into 'running' with a chosen start time.

    Returns the refreshed row — the caller needs the generated subtask_id,
    and the in-memory instance from before the UPDATE still carries None.
    """
    node = dag.nodes[0]
    await store.update_node(
        node.id,
        status="running",
        subtask_id=uuid.uuid4(),
        started_at=when,
    )
    refreshed = await store.get_dag(dag.id)
    return next(n for n in refreshed.nodes if n.id == node.id)


# ---------------------------------------------------------------------------
# Wall-clock reaper
# ---------------------------------------------------------------------------


class TestNodeReaper:
    @pytest.mark.asyncio
    async def test_reaps_node_past_timeout_plus_grace(
        self, store, subtask_mgr, dynamic_loader
    ):
        """timeout 120 + grace 300 = 420s budget; 500s elapsed must fail."""
        dag = await store.create(_subtask_dag())
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=500)
        )
        # Subtask still claims to be running — nobody is coming for it.
        _cancel_takes_effect(subtask_mgr, node.subtask_id)

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        fetched = await store.get_dag(dag.id)
        reaped = fetched.nodes[0]
        assert reaped.status == "failed"
        assert "exceeded wall-clock budget" in reaped.error

    @pytest.mark.asyncio
    async def test_does_not_reap_inside_grace(
        self, store, subtask_mgr, dynamic_loader
    ):
        """The grace exists so the subtask executor reports its own error first."""
        dag = await store.create(_subtask_dag("inside-grace"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=200)
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).nodes[0].status == "running"

    @pytest.mark.asyncio
    async def test_reaper_cancels_primitive_before_marking_failed(
        self, store, subtask_mgr, dynamic_loader
    ):
        """A still-live subtask must stop burning tokens and holding a slot."""
        dag = await store.create(_subtask_dag("cancel-order"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=900)
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        subtask_mgr.cancel.assert_awaited_once_with(node.subtask_id)

    @pytest.mark.asyncio
    async def test_reaper_disabled_by_flag(
        self, store, subtask_mgr, dynamic_loader
    ):
        dag = await store.create(_subtask_dag("reaper-off"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=9000)
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )

        orch = _orch(
            store, subtask_mgr, dynamic_loader,
            _settings(dag_node_reaper_enabled=False),
        )
        await orch.tick()

        assert (await store.get_dag(dag.id)).nodes[0].status == "running"

    @pytest.mark.asyncio
    async def test_node_without_started_at_is_left_alone(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Wall-clock is unknowable without a start time — don't guess."""
        dag = await store.create(_subtask_dag("no-start"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = dag.nodes[0]
        await store.update_node(
            node.id, status="running", subtask_id=uuid.uuid4(), started_at=None
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).nodes[0].status == "running"

    @pytest.mark.asyncio
    async def test_reaped_node_releases_the_active_dag_slot(
        self, store, subtask_mgr, dynamic_loader
    ):
        """The point of the reaper: a wedged DAG must stop occupying a slot."""
        dag = await store.create(_subtask_dag("slot-release"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        _cancel_takes_effect(subtask_mgr, node.subtask_id)
        assert await store.count_active() == 1

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).status == "failed"
        assert await store.count_active() == 0


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------


class TestTokenAccounting:
    @pytest.mark.asyncio
    async def test_completed_subtask_tokens_roll_up(
        self, store, subtask_mgr, dynamic_loader
    ):
        dag = await store.create(_subtask_dag("tokens-dag"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(store, dag, datetime.now(UTC))
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="completed", result="done", error=None,
            final_outcome="completed", tokens_in=400, tokens_out=100,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.tokens_consumed == 500
        assert fetched.nodes[0].tokens_used == 500
        assert fetched.nodes[0].tokens_counted is True

    @pytest.mark.asyncio
    async def test_tokens_counted_exactly_once_across_ticks(
        self, store, subtask_mgr, dynamic_loader
    ):
        """_sync_subtask_node is re-entrant — the add must not repeat."""
        dag = await store.create(_subtask_dag("idempotent-dag"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(store, dag, datetime.now(UTC))
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="completed", result="done", error=None,
            final_outcome="completed", tokens_in=400, tokens_out=100,
        )
        orch = _orch(store, subtask_mgr, dynamic_loader)

        await orch.tick()
        # Force the node back to running so sync re-runs against the same
        # finished subtask, the shape a re-sync would produce.
        await store.update_node(node.id, status="running")
        await orch.tick()

        assert (await store.get_dag(dag.id)).tokens_consumed == 500

    @pytest.mark.asyncio
    async def test_failed_subtask_tokens_still_counted(
        self, store, subtask_mgr, dynamic_loader
    ):
        """A failed node burned real tokens — the budget must see them."""
        dag = await store.create(_subtask_dag("failed-tokens"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(store, dag, datetime.now(UTC))
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="failed", result=None, error="boom",
            final_outcome=None, tokens_in=70, tokens_out=30,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).tokens_consumed == 100

    @pytest.mark.asyncio
    async def test_claim_and_roll_up_are_one_transaction(self, store):
        """@codex P2: a failed roll-up must not leave the node claimed.

        If the claim committed separately, a crash between the two writes
        would mark the node counted forever with nothing added, and every
        later tick would short-circuit on the claim — silently under-counting
        the DAG against its budget.
        """
        dag = await store.create(_subtask_dag("atomic-tokens"))
        node = dag.nodes[0]

        claimed = await store.claim_and_add_node_tokens(node.id, dag.id, 250)
        assert claimed is True

        fetched = await store.get_dag(dag.id)
        assert fetched.tokens_consumed == 250
        assert fetched.nodes[0].tokens_counted is True
        assert fetched.nodes[0].tokens_used == 250

        # Second claim loses and adds nothing.
        assert await store.claim_and_add_node_tokens(node.id, dag.id, 999) is False
        assert (await store.get_dag(dag.id)).tokens_consumed == 250

    @pytest.mark.asyncio
    async def test_in_memory_total_refreshed_for_same_tick_budget_check(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2: the budget check runs later in the SAME _advance_dag
        against the `dag` object loaded before sync. If the roll-up only
        touched the row, a DAG pushed over budget by a just-finished node
        would still dispatch its successor wave this tick."""
        request = DAGCreateRequest(
            name="same-tick-budget",
            token_budget=100,
            nodes=[
                DAGNodeSpec(
                    name="first", type=DAGNodeType.subtask,
                    instructions="one", timeout_seconds=120,
                ),
                DAGNodeSpec(
                    name="second", type=DAGNodeType.subtask,
                    instructions="two", timeout_seconds=120,
                ),
            ],
            edges=[DAGEdgeSpec(from_node="first", to_node="second")],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        first = next(n for n in dag.nodes if n.name == "first")
        await store.update_node(
            first.id, status="running", subtask_id=uuid.uuid4(),
            started_at=datetime.now(UTC),
        )
        # first finishes having blown the whole budget in one go
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="completed", result="done", error=None,
            final_outcome="completed", tokens_in=400, tokens_out=100,
        )

        orch = _orch(
            store, subtask_mgr, dynamic_loader,
            _settings(dag_token_budget_enforcement_enabled=True),
        )
        await orch.tick()

        fetched = await store.get_dag(dag.id)
        second = next(n for n in fetched.nodes if n.name == "second")
        # Successor must NOT have been dispatched on the same tick.
        assert second.status == "cancelled"
        assert fetched.tokens_consumed == 500

    @pytest.mark.asyncio
    async def test_over_budget_but_fully_completed_dag_stays_completed(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 5, resolved at the root.

        A DAG whose nodes all completed has no future work for enforcement to
        stop, so the overage must not relabel it 'partial' — that would claim
        work was skipped when none was. This also makes the final status
        independent of WHEN accounting landed, which is the inconsistency
        codex flagged: late reconciliation now yields the same status as
        accounting that succeeded a tick earlier.
        """
        request = DAGCreateRequest(
            name="over-budget-complete",
            token_budget=100,
            nodes=[
                DAGNodeSpec(
                    name="only", type=DAGNodeType.subtask,
                    instructions="work", timeout_seconds=120,
                ),
            ],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(store, dag, datetime.now(UTC))
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="completed", result="done", error=None,
            final_outcome="completed", tokens_in=400, tokens_out=100,
        )

        orch = _orch(
            store, subtask_mgr, dynamic_loader,
            _settings(dag_token_budget_enforcement_enabled=True),
        )
        await orch.tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.tokens_consumed == 500  # overage is recorded
        assert fetched.status == "completed"   # ...but nothing was curtailed
        assert fetched.nodes[0].status == "completed"

    @pytest.mark.asyncio
    async def test_prior_curtailment_survives_across_ticks(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 6: 'did enforcement curtail anything' is a property
        of the DAG's history, not of this tick. A wave with running work AND
        pending successors cancels the successors on tick 1 and returns; on
        tick 2 nothing is left to cancel, and a tick-local flag would decline
        and let the DAG be labelled 'cancelled' instead of 'partial'."""
        request = DAGCreateRequest(
            name="curtail-memory",
            token_budget=100,
            nodes=[
                DAGNodeSpec(
                    name="first", type=DAGNodeType.subtask,
                    instructions="one", timeout_seconds=120,
                ),
                DAGNodeSpec(
                    name="second", type=DAGNodeType.subtask,
                    instructions="two", timeout_seconds=120,
                ),
            ],
            edges=[DAGEdgeSpec(from_node="first", to_node="second")],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        first = next(n for n in dag.nodes if n.name == "first")
        second = next(n for n in dag.nodes if n.name == "second")
        # Tick-1 aftermath: successor already cancelled by enforcement, the
        # running node has since finished.
        await store.update_node(
            second.id, status="cancelled", error="Token budget exceeded",
        )
        await store.update_node(
            first.id, status="running", subtask_id=uuid.uuid4(),
            started_at=datetime.now(UTC),
        )
        await store.update_dag_tokens(dag.id, 500)
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="completed", result="done", error=None,
            final_outcome="completed", tokens_in=0, tokens_out=0,
        )

        orch = _orch(
            store, subtask_mgr, dynamic_loader,
            _settings(dag_token_budget_enforcement_enabled=True),
        )
        await orch.tick()

        fetched = await store.get_dag(dag.id)
        # Enforcement DID curtail work earlier, so 'partial', not 'cancelled'.
        assert fetched.status == "partial"
        assert "budget" in (fetched.result_summary or "").lower()

    @pytest.mark.asyncio
    async def test_budget_not_enforced_while_flag_is_off(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Accounting live + enforcement dark is the deliberate default."""
        request = DAGCreateRequest(
            name="budget-dark",
            token_budget=100,
            nodes=[
                DAGNodeSpec(
                    name="first", type=DAGNodeType.subtask,
                    instructions="one", timeout_seconds=120,
                ),
                DAGNodeSpec(
                    name="second", type=DAGNodeType.subtask,
                    instructions="two", timeout_seconds=120,
                ),
            ],
            edges=[DAGEdgeSpec(from_node="first", to_node="second")],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "running")
        await store.update_dag_tokens(dag.id, 500)
        dag = await store.get_dag(dag.id)
        second = next(n for n in dag.nodes if n.name == "second")

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        fetched = await store.get_dag(dag.id)
        refetched = next(n for n in fetched.nodes if n.id == second.id)
        assert refetched.status != "cancelled"


class TestRetryResetsPerAttemptState:
    """@codex P2 round 2: every path that re-runs a node must reset the
    per-attempt bookkeeping, or the retry's cost and outcome go unrecorded."""

    @pytest.mark.asyncio
    async def test_retry_node_clears_token_claim_and_accumulates(
        self, store, subtask_mgr, dynamic_loader
    ):
        dag = await store.create(_subtask_dag("retry-tokens"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(store, dag, datetime.now(UTC))

        # Attempt 1 fails having burned 300 tokens.
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="failed", result=None, error="boom",
            final_outcome=None, tokens_in=200, tokens_out=100,
        )
        orch = _orch(store, subtask_mgr, dynamic_loader)
        await orch.tick()
        assert (await store.get_dag(dag.id)).tokens_consumed == 300

        # Operator retries the node.
        await orch.retry_node(dag.id, "long-runner")
        refetched = await store.get_dag(dag.id)
        assert refetched.nodes[0].tokens_counted is False

        # Attempt 2 succeeds having burned 150 more.
        node2 = await _running_node_started_at(
            store, refetched, datetime.now(UTC)
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node2.subtask_id, status="completed", result="ok", error=None,
            final_outcome="completed", tokens_in=100, tokens_out=50,
        )
        await orch.tick()

        final = await store.get_dag(dag.id)
        # Both attempts really were paid for.
        assert final.tokens_consumed == 450
        # And the node agrees with the DAG it rolls up into.
        assert final.nodes[0].tokens_used == 450

    @pytest.mark.asyncio
    async def test_old_attempt_is_banked_before_its_claim_is_reset(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 8: retry_node clears tokens_counted AND drops
        subtask_id, so an attempt that was never accounted becomes invisible —
        reconciliation afterwards can only see the replacement subtask."""
        dag = await store.create(_subtask_dag("bank-before-retry"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = dag.nodes[0]
        await store.update_node(
            node.id, status="failed", subtask_id=uuid.uuid4(),
            error="boom", tokens_counted=False,
        )
        # The first attempt really did burn tokens; nothing ever counted them.
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="failed", result=None, error="boom",
            final_outcome=None, tokens_in=250, tokens_out=150,
        )

        await _orch(store, subtask_mgr, dynamic_loader).retry_node(
            dag.id, "long-runner"
        )

        fetched = await store.get_dag(dag.id)
        retried = fetched.nodes[0]
        # Attempt 1's usage was banked before the reset...
        assert fetched.tokens_consumed == 400
        assert retried.tokens_used == 400
        # ...and the node is still ready to count attempt 2 on top.
        assert retried.tokens_counted is False
        assert retried.status == "pending"
        assert retried.subtask_id is None

    @pytest.mark.asyncio
    async def test_downstream_unblocked_attempt_is_banked_too(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 9: the THIRD reset site. retry_node also unblocks
        reachable descendants and clears their claims — a cancel_cascade child
        may have been running (and consuming) when it was cancelled."""
        request = DAGCreateRequest(
            name="downstream-bank",
            nodes=[
                DAGNodeSpec(
                    name="parent", type=DAGNodeType.subtask,
                    instructions="p", timeout_seconds=120,
                ),
                DAGNodeSpec(
                    name="child", type=DAGNodeType.subtask,
                    instructions="c", timeout_seconds=120,
                ),
            ],
            edges=[
                DAGEdgeSpec(
                    from_node="parent", to_node="child",
                    edge_type="cancel_cascade",
                ),
            ],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        parent = next(n for n in dag.nodes if n.name == "parent")
        child = next(n for n in dag.nodes if n.name == "child")
        await store.update_node(parent.id, status="failed", error="boom")
        # Child was running and consuming when the cascade cancelled it.
        await store.update_node(
            child.id, status="cancelled", subtask_id=uuid.uuid4(),
            error="Cancelled by predecessor failure", tokens_counted=False,
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="cancelled", result=None, error=None,
            final_outcome="cancelled", tokens_in=90, tokens_out=10,
        )

        await _orch(store, subtask_mgr, dynamic_loader).retry_node(
            dag.id, "parent"
        )

        fetched = await store.get_dag(dag.id)
        refetched_child = next(n for n in fetched.nodes if n.name == "child")
        # The child's cancelled attempt was banked before its claim reset.
        assert fetched.tokens_consumed == 100
        assert refetched_child.tokens_used == 100
        assert refetched_child.tokens_counted is False
        assert refetched_child.status == "pending"

    @pytest.mark.asyncio
    async def test_retry_refuses_when_banking_fails(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 10: banking was best-effort while the reset was
        unconditional, so a transient failure lost the attempt permanently.
        Refusing is better — the operator can simply try again."""
        dag = await store.create(_subtask_dag("banking-fails"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = dag.nodes[0]
        await store.update_node(
            node.id, status="failed", subtask_id=uuid.uuid4(),
            error="boom", tokens_counted=False,
        )
        subtask_mgr.get.side_effect = RuntimeError("db unreachable")

        orch = _orch(store, subtask_mgr, dynamic_loader)
        with pytest.raises(ValueError, match="token usage"):
            await orch.retry_node(dag.id, "long-runner")

        # Nothing was reset — the old attempt is still reachable.
        fetched = await store.get_dag(dag.id)
        assert fetched.nodes[0].status == "failed"
        assert fetched.nodes[0].subtask_id is not None

    @pytest.mark.asyncio
    async def test_fix_stage_proceeds_when_banking_fails(
        self, store, subtask_mgr, dynamic_loader
    ):
        """The fix stage must NOT refuse when banking fails.

        An earlier version deferred here, and CI caught the consequence: the
        fix stage is automatic and re-fires every tick, so a subtask that
        never settles deferred forever — parent stuck 'failed', fix node never
        terminal, DAG never finalizable. A permanent wedge is strictly worse
        than an under-counted token total, so automatic paths always make
        progress and log the loss at ERROR. Only retry_node refuses.
        """
        request = DAGCreateRequest(
            name="fix-defer-free",
            nodes=[
                DAGNodeSpec(
                    name="work", type=DAGNodeType.subtask,
                    instructions="do it", timeout_seconds=120,
                ),
                DAGNodeSpec(
                    name="fix-work", type=DAGNodeType.fix,
                    instructions="recover", parent_node="work",
                    fix_actions=["retry_as_is"],
                ),
            ],
            edges=[
                DAGEdgeSpec(
                    from_node="work", to_node="fix-work",
                    edge_type="on_failure",
                ),
            ],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        await store.update_node(
            work.id, status="failed", subtask_id=uuid.uuid4(),
            error="subtask incomplete_no_terminal: ran out of turns",
            tokens_counted=False,
        )
        subtask_mgr.get.side_effect = RuntimeError("db unreachable")

        orch = _orch(store, subtask_mgr, dynamic_loader)
        dag = await store.get_dag(dag.id)
        await orch._try_fix_failed_nodes(dag)

        fetched = await store.get_dag(dag.id)
        fix_node = next(n for n in fetched.nodes if n.name == "fix-work")
        parent = next(n for n in fetched.nodes if n.name == "work")
        # The DAG makes progress rather than wedging: the parent is re-queued
        # and the fix node reaches terminal so completion can finalize.
        assert parent.status == "pending"
        assert fix_node.status == "completed"
        assert fix_node.fix_attempts_used == 1

    @pytest.mark.asyncio
    async def test_retry_node_reopens_delivery(
        self, store, subtask_mgr, dynamic_loader
    ):
        """A reactivated DAG must be announced again when it re-finishes."""
        dag = await store.create(_subtask_dag("retry-delivery"))
        await store.update_dag_status(dag.id, "failed", result_summary="v1")
        await store.mark_delivered(dag.id, dag.delivery_generation)
        await store.save_delivery_summary(dag.id, dag.delivery_generation, "first outcome summary")
        await store.update_node(dag.nodes[0].id, status="failed", error="boom")

        assert (await store.get_dag(dag.id)).delivered_at is not None

        orch = _orch(store, subtask_mgr, dynamic_loader)
        await orch.retry_node(dag.id, "long-runner")

        reopened = await store.get_dag(dag.id)
        assert reopened.status == "running"
        assert reopened.delivered_at is None
        assert reopened.delivery_attempts == 0
        assert reopened.delivery_error is None
        # The cached summary described the PREVIOUS outcome.
        assert reopened.delivery_summary is None

        # It is now visible to the sweep again.
        pending = await store.get_undelivered_terminal_dags()
        await store.update_dag_status(dag.id, "completed", result_summary="v2")
        pending = await store.get_undelivered_terminal_dags()
        assert dag.id in {d.id for d in pending}

    @pytest.mark.asyncio
    async def test_transient_accounting_failure_is_reconciled_later(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 3: _count_node_tokens swallows its errors so a
        bookkeeping failure can't derail the status sync — but the node then
        terminalizes, and later ticks only sync 'running' nodes. Without a
        reconciliation sweep one transient DB blip stranded those tokens."""
        dag = await store.create(_subtask_dag("reconcile-tokens"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(store, dag, datetime.now(UTC))
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="completed", result="ok", error=None,
            final_outcome="completed", tokens_in=200, tokens_out=100,
        )
        orch = _orch(store, subtask_mgr, dynamic_loader)

        # Tick 1: the roll-up blows up transiently. The node still terminalizes.
        original = store.claim_and_add_node_tokens
        store.claim_and_add_node_tokens = AsyncMock(
            side_effect=RuntimeError("connection reset")
        )
        await orch.tick()

        after_failure = await store.get_dag(dag.id)
        assert after_failure.nodes[0].status == "completed"
        assert after_failure.nodes[0].tokens_counted is False
        assert after_failure.tokens_consumed == 0

        # Tick 2: DB recovers — the sweep picks the stranded node back up.
        store.claim_and_add_node_tokens = original
        await store.update_dag_status(dag.id, "running")  # keep it sweep-visible
        await orch.tick()

        recovered = await store.get_dag(dag.id)
        assert recovered.tokens_consumed == 300
        assert recovered.nodes[0].tokens_counted is True

    @pytest.mark.asyncio
    async def test_terminal_dag_is_reconciled_before_its_outcome_is_announced(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 4: _advance_dag only runs for pending/running DAGs,
        so a DAG terminalized on the same tick its accounting failed never got
        the promised retry. Reconcile at the last point before publishing."""
        dag = await store.create(_subtask_dag("terminal-reconcile"))
        node = dag.nodes[0]
        # Terminal DAG, undelivered, with a node whose tokens never landed.
        await store.update_node(
            node.id, status="completed", subtask_id=uuid.uuid4(),
            result="ok", tokens_counted=False,
        )
        await store.update_dag_status(dag.id, "completed", result_summary="done")
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="completed", result="ok", error=None,
            final_outcome="completed", tokens_in=700, tokens_out=300,
        )

        delivery = AsyncMock()
        delivery.deliver.return_value = SimpleNamespace(
            delivered=True, legs=(), summary="ok", summary_authored=False,
        )
        orch = DAGOrchestrator(
            store=store, subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader, settings=_settings(),
            delivery=delivery,
        )
        orch.clock_wired = True
        await orch.tick()
        # The sweep is detached from the tick, so drain it before asserting.
        await orch.wait_for_delivery()

        fetched = await store.get_dag(dag.id)
        assert fetched.tokens_consumed == 1000
        assert fetched.nodes[0].tokens_counted is True
        # And the DAG handed to deliver() already carried the corrected total.
        announced = delivery.deliver.await_args.args[0]
        assert announced.tokens_consumed == 1000

    @pytest.mark.asyncio
    async def test_claim_deferred_while_subtask_still_running(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 7: cancel_dag, failure propagation and the reaper
        all terminalize a node while its worker may still be executing. The
        claim is one-shot, so freezing the current counters would make them
        permanently final — no later value could replace them."""
        dag = await store.create(_subtask_dag("live-subtask"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = dag.nodes[0]
        # Node reaped/cancelled, but the worker is still going.
        await store.update_node(
            node.id, status="failed", subtask_id=uuid.uuid4(),
            error="stalled", started_at=datetime.now(UTC),
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )
        orch = _orch(store, subtask_mgr, dynamic_loader)

        await orch.tick()
        assert (await store.get_dag(dag.id)).nodes[0].tokens_counted is False

        # Worker settles with its real usage — now the claim lands.
        subtask_mgr.get.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="failed", result=None, error="boom",
            final_outcome=None, tokens_in=600, tokens_out=200,
        )
        await store.update_dag_status(dag.id, "running")
        await orch.tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.nodes[0].tokens_counted is True
        assert fetched.tokens_consumed == 800

    @pytest.mark.asyncio
    async def test_reconciliation_claims_zero_when_subtask_is_gone(
        self, store, subtask_mgr, dynamic_loader
    ):
        """A deleted subtask's usage is unknowable — claim zero so the sweep
        stops spinning every tick for the life of the DAG."""
        dag = await store.create(_subtask_dag("gone-subtask"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = dag.nodes[0]
        await store.update_node(
            node.id, status="failed", subtask_id=uuid.uuid4(), error="boom",
        )
        subtask_mgr.get.return_value = None

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.nodes[0].tokens_counted is True
        assert fetched.tokens_consumed == 0

    @pytest.mark.asyncio
    async def test_reactivation_and_delivery_reset_are_one_transaction(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex P2 round 3: a crash between a committed 'running' and a
        separate delivery reset leaves a LIVE DAG carrying a stale
        delivered_at — it finishes normally and is then never announced."""
        dag = await store.create(_subtask_dag("atomic-reactivate"))
        await store.update_dag_status(dag.id, "failed", result_summary="v1")
        await store.mark_delivered(dag.id, dag.delivery_generation)
        await store.save_delivery_summary(dag.id, dag.delivery_generation, "old summary")
        await store.update_node(dag.nodes[0].id, status="failed", error="boom")

        await _orch(store, subtask_mgr, dynamic_loader).retry_node(
            dag.id, "long-runner"
        )

        fetched = await store.get_dag(dag.id)
        # Both halves landed — never one without the other.
        assert fetched.status == "running"
        assert fetched.delivered_at is None
        assert fetched.delivery_summary is None
        assert fetched.delivery_attempts == 0

    @pytest.mark.asyncio
    async def test_retry_on_cancelled_dag_refuses_instead_of_dead_ending(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Pre-existing silent dead end, found while auditing the retry paths.

        Only failed/partial DAGs get reactivated, and get_active_dags() serves
        only pending/running — so this reported success, left the node
        'pending', and the tick loop never touched the DAG again.
        """
        dag = await store.create(_subtask_dag("cancelled-retry"))
        await store.update_dag_status(dag.id, "running")
        await store.update_node(dag.nodes[0].id, status="failed", error="boom")
        await store.update_dag_status(dag.id, "cancelled")

        orch = _orch(store, subtask_mgr, dynamic_loader)
        with pytest.raises(ValueError, match="cancelled"):
            await orch.retry_node(dag.id, "long-runner")

        # Refused BEFORE mutating — no half-reset node left behind.
        fetched = await store.get_dag(dag.id)
        assert fetched.nodes[0].status == "failed"
        assert fetched.status == "cancelled"

    @pytest.mark.asyncio
    async def test_fix_stage_retry_clears_token_claim(self, store):
        """The automatic sibling of retry_node had the identical bug."""
        request = DAGCreateRequest(
            name="fix-retry-tokens",
            nodes=[
                DAGNodeSpec(
                    name="work", type=DAGNodeType.subtask,
                    instructions="do it", timeout_seconds=120,
                ),
                DAGNodeSpec(
                    name="fix-work", type=DAGNodeType.fix,
                    instructions="recover", parent_node="work",
                    fix_actions=["retry_as_is"],
                ),
            ],
            edges=[
                DAGEdgeSpec(
                    from_node="work", to_node="fix-work",
                    edge_type="on_failure",
                ),
            ],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        work = next(n for n in dag.nodes if n.name == "work")
        await store.update_node(
            work.id,
            # choose_action keys retry_as_is off this exact token.
            status="failed",
            error="subtask incomplete_no_terminal: ran out of turns",
            tokens_counted=True,
        )

        subtask_mgr = AsyncMock()
        subtask_mgr.create.return_value = SimpleNamespace(
            id=uuid.uuid4(), status="pending"
        )
        loader = AsyncMock()
        loader._registry = MagicMock()
        loader._registry.get_check.return_value = None
        orch = _orch(store, subtask_mgr, loader)
        dag = await store.get_dag(dag.id)
        await orch._try_fix_failed_nodes(dag)

        refetched = await store.get_dag(dag.id)
        retried = next(n for n in refetched.nodes if n.name == "work")
        assert retried.status == "pending"
        assert retried.tokens_counted is False


# ---------------------------------------------------------------------------
# Fail-loud wiring
# ---------------------------------------------------------------------------


class TestClockWiring:
    @pytest.mark.asyncio
    async def test_clock_wired_defaults_false(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Guilty until proven innocent: only whoever installs the tick
        may declare the orchestrator wired."""
        orch = DAGOrchestrator(
            store=store, subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader, settings=_settings(),
        )
        assert orch.clock_wired is False

    @pytest.mark.asyncio
    async def test_dag_create_refuses_when_clock_unwired(
        self, store, subtask_mgr, dynamic_loader
    ):
        orch = DAGOrchestrator(
            store=store, subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader, settings=_settings(),
        )
        dispatcher = ToolDispatcher()
        register_dag_tools(dispatcher, store, orch)

        result = await dispatcher._handlers["dag_create"](
            name="doomed",
            nodes=[{"name": "a", "type": "subtask", "instructions": "x"}],
            edges=[],
        )

        text = result["content"][0]["text"]
        assert "not wired" in text
        assert "NOUS_HEARTBEAT_ENABLED" in text
        # And nothing was persisted.
        assert await store.count_active() == 0

    @pytest.mark.asyncio
    async def test_dag_manage_list_warns_when_clock_unwired(
        self, store, subtask_mgr, dynamic_loader
    ):
        """'nothing is running' and 'nothing can ever run' must read differently."""
        orch = DAGOrchestrator(
            store=store, subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader, settings=_settings(),
        )
        dispatcher = ToolDispatcher()
        register_dag_tools(dispatcher, store, orch)

        result = await dispatcher._handlers["dag_manage"](action="list")

        assert "not wired" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_dag_create_succeeds_once_wired(
        self, store, subtask_mgr, dynamic_loader
    ):
        orch = _orch(store, subtask_mgr, dynamic_loader)
        dispatcher = ToolDispatcher()
        register_dag_tools(dispatcher, store, orch)

        result = await dispatcher._handlers["dag_create"](
            name="fine",
            nodes=[{"name": "a", "type": "subtask", "instructions": "x"}],
            edges=[],
        )

        assert "Error" not in result["content"][0]["text"]
        assert await store.count_active() == 1

    @pytest.mark.asyncio
    async def test_tick_records_last_tick_at(
        self, store, subtask_mgr, dynamic_loader
    ):
        orch = _orch(store, subtask_mgr, dynamic_loader)
        assert orch.last_tick_at is None
        await orch.tick()
        assert orch.last_tick_at is not None


class TestRoundElevenGuards:
    """@codex round 11 — three gaps in areas the earlier rounds never reached."""

    @pytest.mark.asyncio
    async def test_reaper_spares_a_still_queued_subtask(
        self, store, subtask_mgr, dynamic_loader
    ):
        """node.started_at is set at DISPATCH; the subtask sits 'pending' until
        a worker dequeues it. Reaping on elapsed-since-dispatch alone kills
        work that never started."""
        dag = await store.create(_subtask_dag("queued-not-reaped"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        # Way past timeout+grace, but the worker pool never picked it up.
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="pending", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).nodes[0].status == "running"
        subtask_mgr.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_dag_is_a_noop_on_a_partial_dag(
        self, store, subtask_mgr, dynamic_loader
    ):
        """partial is terminal and lives in the delivery sweep's domain, so
        cancelling it would rewrite a terminal outcome behind an in-flight
        delivery's back."""
        dag = await store.create(_subtask_dag("partial-cancel"))
        await store.update_dag_status(
            dag.id, "partial", result_summary="budget exceeded, partial"
        )

        await _orch(store, subtask_mgr, dynamic_loader).cancel_dag(
            dag.id, reason="user asked"
        )

        fetched = await store.get_dag(dag.id)
        assert fetched.status == "partial"
        assert fetched.result_summary == "budget exceeded, partial"

    @pytest.mark.asyncio
    async def test_reaper_measures_from_execution_not_dispatch(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 12: sparing 'pending' was a half-measure. The moment a
        worker dequeues a long-queued task its status flips to 'running' while
        node.started_at still holds the DISPATCH time, so the next tick reaped
        work that had just begun."""
        dag = await store.create(_subtask_dag("exec-clock"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        # Dispatched 5000s ago — far past timeout(120)+grace(300)...
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        # ...but it only got dequeued 10s ago and is genuinely executing.
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
            started_at=datetime.now(UTC) - timedelta(seconds=10),
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).nodes[0].status == "running"
        subtask_mgr.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaper_still_fires_on_the_execution_clock(
        self, store, subtask_mgr, dynamic_loader
    ):
        """The backstop must still work — a task genuinely executing past
        timeout+grace is reaped regardless of when it was dispatched."""
        dag = await store.create(_subtask_dag("exec-clock-fires"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        _cancel_takes_effect(
            subtask_mgr, node.subtask_id,
            exec_started=datetime.now(UTC) - timedelta(seconds=900),
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        reaped = (await store.get_dag(dag.id)).nodes[0]
        assert reaped.status == "failed"
        assert "exceeded wall-clock budget" in reaped.error

    @pytest.mark.asyncio
    async def test_reaper_keeps_a_result_that_landed_during_cancellation(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 13: the subtask can complete between the status read
        and the cancel. cancel() refuses to touch a terminal row, so the work
        succeeded — failing the node would discard a real result."""
        dag = await store.create(_subtask_dag("raced-completion"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        long_ago = datetime.now(UTC) - timedelta(seconds=5000)
        calls = {"n": 0}

        async def _completes_during_cancel(_sid):
            calls["n"] += 1
            # First read (reaper's status check): still running and overrun.
            if calls["n"] == 1:
                return SimpleNamespace(
                    id=node.subtask_id, status="running", result=None,
                    error=None, final_outcome=None, tokens_in=0, tokens_out=0,
                    started_at=long_ago,
                )
            # By the time we cancel, it had already finished successfully.
            return SimpleNamespace(
                id=node.subtask_id, status="completed", result="the answer",
                error=None, final_outcome="completed",
                tokens_in=10, tokens_out=5, started_at=long_ago,
            )

        subtask_mgr.get = AsyncMock(side_effect=_completes_during_cancel)

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        fetched = await store.get_dag(dag.id)
        kept = fetched.nodes[0]
        assert kept.status == "completed"
        assert kept.result == "the answer"
        assert fetched.status == "completed"

    @pytest.mark.asyncio
    async def test_reaper_defers_when_the_execution_clock_lookup_fails(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 17: swallowing the lookup error to None fell through
        and reaped on the DISPATCH clock — re-creating the exact false positive
        the execution-clock check exists to prevent, on a transient DB blip."""
        dag = await store.create(_subtask_dag("lookup-fails"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        subtask_mgr.get.side_effect = RuntimeError("db blip")

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).nodes[0].status == "running"
        subtask_mgr.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaper_keeps_a_real_failure_that_wins_the_race(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 17: the race guard covered only 'completed'. A subtask
        going running -> failed in the same window is equally terminal, and
        falling through replaced its real error with the generic wall-clock
        message — lost permanently, since terminal nodes are never re-synced."""
        dag = await store.create(_subtask_dag("raced-failure"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        long_ago = datetime.now(UTC) - timedelta(seconds=5000)
        calls = {"n": 0}

        async def _fails_during_cancel(_sid):
            calls["n"] += 1
            if calls["n"] == 1:
                return SimpleNamespace(
                    id=node.subtask_id, status="running", result=None,
                    error=None, final_outcome=None, tokens_in=0, tokens_out=0,
                    started_at=long_ago,
                )
            return SimpleNamespace(
                id=node.subtask_id, status="failed", result=None,
                error="upstream API returned 401", final_outcome=None,
                tokens_in=40, tokens_out=10, started_at=long_ago,
            )

        subtask_mgr.get = AsyncMock(side_effect=_fails_during_cancel)

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        reaped = (await store.get_dag(dag.id)).nodes[0]
        assert reaped.status == "failed"
        # The primitive's own reason survives, not the generic timeout text.
        assert "401" in reaped.error
        assert "wall-clock" not in reaped.error

    @pytest.mark.asyncio
    async def test_reaper_defers_when_the_post_cancel_confirmation_fails(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 18: a FAILED confirmation read is not proof the reaper
        won. Swallowing it to None overwrote the primitive's real terminal
        outcome with the generic wall-clock error.

        Deferring converges: the subtask is already cancelled, so a later tick
        either syncs its persisted outcome or reaps once the read succeeds.
        """
        dag = await store.create(_subtask_dag("confirm-fails"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        long_ago = datetime.now(UTC) - timedelta(seconds=5000)
        calls = {"n": 0}

        async def _confirmation_blips(_sid):
            calls["n"] += 1
            if calls["n"] == 1:  # pre-cancel read succeeds
                return SimpleNamespace(
                    id=node.subtask_id, status="running", result=None,
                    error=None, final_outcome=None, tokens_in=0, tokens_out=0,
                    started_at=long_ago,
                )
            raise RuntimeError("db blip during confirmation")

        subtask_mgr.get = AsyncMock(side_effect=_confirmation_blips)

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        # Not overwritten with the generic error — left for the next tick.
        still = (await store.get_dag(dag.id)).nodes[0]
        assert still.status == "running"
        assert still.error is None

    @pytest.mark.asyncio
    async def test_reaper_defers_while_cancellation_has_not_taken(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 19: _cancel_node swallows its errors, so a confirmation
        read of 'running' means cancellation did NOT take. Failing the node
        there abandons a live worker whose result is then discarded, since
        terminal nodes are never re-synced."""
        dag = await store.create(_subtask_dag("cancel-didnt-take"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        long_ago = datetime.now(UTC) - timedelta(seconds=5000)
        # Cancellation never takes: the row stays 'running' throughout.
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
            started_at=long_ago,
        )
        subtask_mgr.cancel.side_effect = RuntimeError("cancel failed")
        orch = _orch(store, subtask_mgr, dynamic_loader)

        await orch.tick()
        assert (await store.get_dag(dag.id)).nodes[0].status == "running"

    @pytest.mark.asyncio
    async def test_reaper_deferral_is_bounded_so_orphans_still_die(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Unbounded deferral would re-create the wedge the reaper exists to
        prevent: an orphaned row whose worker died never settles."""
        dag = await store.create(_subtask_dag("orphan-eventually-reaped"))
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        node = await _running_node_started_at(
            store, dag, datetime.now(UTC) - timedelta(seconds=5000)
        )
        long_ago = datetime.now(UTC) - timedelta(seconds=5000)
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
            started_at=long_ago,
        )
        subtask_mgr.cancel.side_effect = RuntimeError("cancel failed")
        orch = _orch(store, subtask_mgr, dynamic_loader)

        for _ in range(6):
            await orch.tick()
            await store.update_dag_status(dag.id, "running")

        reaped = (await store.get_dag(dag.id)).nodes[0]
        assert reaped.status == "failed"
        # The error is honest about what could not be confirmed.
        assert "cancellation could not be confirmed" in reaped.error
        assert "may still be running" in reaped.error

    @pytest.mark.asyncio
    async def test_retry_is_atomic_no_half_mutated_state_is_observable(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 20: reordering only MOVED the window — reactivation
        last let the sweep announce half-reset nodes; reactivation first let a
        tick load a running DAG whose nodes were all still terminal, re-finalize
        it, and strand a pending node in a terminal DAG. The split itself is
        the invalid state, so it must be one transaction."""
        request = DAGCreateRequest(
            name="atomic-retry",
            nodes=[
                DAGNodeSpec(name="a", type=DAGNodeType.subtask,
                            instructions="a", timeout_seconds=120),
                DAGNodeSpec(name="b", type=DAGNodeType.subtask,
                            instructions="b", timeout_seconds=120),
            ],
            edges=[DAGEdgeSpec(from_node="a", to_node="b")],
        )
        dag = await store.create(request)
        await store.update_dag_status(dag.id, "failed", result_summary="v1")
        dag = await store.get_dag(dag.id)
        a = next(n for n in dag.nodes if n.name == "a")
        b = next(n for n in dag.nodes if n.name == "b")
        await store.update_node(a.id, status="failed", error="boom")
        await store.update_node(b.id, status="blocked", error="Predecessor failed")

        await _orch(store, subtask_mgr, dynamic_loader).retry_node(dag.id, "a")

        fetched = await store.get_dag(dag.id)
        # Status and node set agree: running DAG, both nodes re-queued.
        assert fetched.status == "running"
        assert {n.status for n in fetched.nodes} == {"pending"}
        # And the delivery state was reset in the SAME transition.
        assert fetched.delivered_at is None
        assert fetched.delivery_generation == 1

    @pytest.mark.asyncio
    async def test_reaper_defers_when_a_check_cannot_be_disabled(
        self, store, subtask_mgr, dynamic_loader
    ):
        """@codex round 20: the cancellation confirmation was subtask-only, so
        a check node whose disable failed was marked failed while the dynamic
        check kept running untracked."""
        dag = await store.create(
            DAGCreateRequest(
                name="check-cancel-fails",
                nodes=[
                    DAGNodeSpec(name="watcher", type=DAGNodeType.check,
                                instructions="watch", timeout_seconds=120),
                ],
            )
        )
        await store.update_dag_status(dag.id, "running")
        dag = await store.get_dag(dag.id)
        await store.update_node(
            dag.nodes[0].id, status="running", check_name="dag-x-watcher",
            started_at=datetime.now(UTC) - timedelta(seconds=5000),
        )
        # Disable fails, and the registry keeps reporting the check as active.
        dynamic_loader.manage_check.side_effect = RuntimeError("db down")
        dynamic_loader._registry.get_check.return_value = SimpleNamespace(
            active=True
        )

        await _orch(store, subtask_mgr, dynamic_loader).tick()

        assert (await store.get_dag(dag.id)).nodes[0].status == "running"
