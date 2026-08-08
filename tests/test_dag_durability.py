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
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )

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
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id, status="running", result=None, error=None,
            final_outcome=None, tokens_in=0, tokens_out=0,
        )
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
