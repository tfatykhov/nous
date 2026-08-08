"""Tests for F087 — durable DAG result delivery.

Before F087 a DAG reaching a terminal status wrote result_summary to a row
and stopped: `self._bus` was assigned in DAGOrchestrator.__init__ and never
read, and grep for emit/publish across nous/dag/ returned nothing. These
tests pin the two properties that closes:

1. Legs are independent — a failing bus or a failing summary never
   suppresses the Telegram push the user actually sees.
2. Delivery is at-least-once — because `delivered_at` is written in a
   separate transaction from the terminal status, a process that dies
   between the two re-delivers on the next tick rather than losing the
   notification. The bus cannot provide this: EventBus.emit drops on
   QueueFull by design.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.delivery import DAGResultDelivery
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    """Hermetic settings — never inherit the developer's .env."""
    base = dict(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
    )
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-delivery-{uuid.uuid4().hex[:8]}", _settings())


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


def _ok_http() -> MagicMock:
    """httpx client stub whose POST succeeds."""
    client = MagicMock()
    client.post = AsyncMock(return_value=SimpleNamespace(status_code=200))
    return client


def _failing_http(status_code: int = 500) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(return_value=SimpleNamespace(status_code=status_code))
    return client


def _callback_dag(name: str = "delivery-dag") -> DAGCreateRequest:
    """Single callback node — reaches terminal on the first tick."""
    return DAGCreateRequest(
        name=name,
        nodes=[
            DAGNodeSpec(
                name="notify",
                type=DAGNodeType.callback,
                instructions="Report the finding",
            ),
        ],
    )


async def _finished_dag(store: DAGStore, name: str = "delivery-dag"):
    """A DAG already in a terminal state, not yet delivered."""
    dag = await store.create(_callback_dag(name))
    await store.update_dag_status(dag.id, "completed", result_summary="All good")
    return await store.get_dag(dag.id)


# ---------------------------------------------------------------------------
# Legs
# ---------------------------------------------------------------------------


class TestDeliveryLegs:
    @pytest.mark.asyncio
    async def test_all_legs_succeed(self, store):
        dag = await _finished_dag(store)
        bus = AsyncMock()
        delivery = DAGResultDelivery(
            _settings(), agent_id="a", bus=bus, http=_ok_http()
        )

        outcome = await delivery.deliver(dag)

        assert outcome.delivered is True
        assert bus.emit.await_count == 1
        assert bus.emit.await_args.args[0].type == "dag.completed"

    @pytest.mark.asyncio
    async def test_failed_dag_emits_dag_failed(self, store):
        dag = await store.create(_callback_dag("failed-dag"))
        await store.update_dag_status(dag.id, "failed", result_summary="nope")
        dag = await store.get_dag(dag.id)

        bus = AsyncMock()
        delivery = DAGResultDelivery(
            _settings(), agent_id="a", bus=bus, http=_ok_http()
        )
        await delivery.deliver(dag)

        assert bus.emit.await_args.args[0].type == "dag.failed"

    @pytest.mark.asyncio
    async def test_bus_failure_does_not_block_telegram(self, store):
        """A down bus must not cost the user their notification."""
        dag = await _finished_dag(store)
        bus = AsyncMock()
        bus.emit.side_effect = RuntimeError("queue exploded")
        http = _ok_http()

        delivery = DAGResultDelivery(_settings(), agent_id="a", bus=bus, http=http)
        outcome = await delivery.deliver(dag)

        assert http.post.await_count == 1
        # Bus is best-effort, so the DAG is still considered delivered.
        assert outcome.delivered is True
        bus_leg = next(leg for leg in outcome.legs if leg.name == "bus")
        assert bus_leg.ok is False
        assert bus_leg.required is False

    @pytest.mark.asyncio
    async def test_telegram_http_error_fails_delivery(self, store):
        """Telegram is the leg the user sees — a failure must bring the sweep back."""
        dag = await _finished_dag(store)
        delivery = DAGResultDelivery(
            _settings(), agent_id="a", bus=AsyncMock(), http=_failing_http(503)
        )

        outcome = await delivery.deliver(dag)

        assert outcome.delivered is False
        assert "telegram" in outcome.failure_detail

    @pytest.mark.asyncio
    async def test_telegram_unconfigured_is_not_required(self, store):
        """Retrying against a channel that does not exist would loop to the cap."""
        dag = await _finished_dag(store)
        delivery = DAGResultDelivery(
            _settings(telegram_bot_token="", telegram_chat_id=""),
            agent_id="a",
            bus=AsyncMock(),
        )

        outcome = await delivery.deliver(dag)

        assert outcome.delivered is True
        leg = next(leg for leg in outcome.legs if leg.name == "telegram")
        assert leg.required is False

    @pytest.mark.asyncio
    async def test_telegram_message_is_truncated(self, store):
        """Telegram rejects messages over 4096 chars."""
        dag = await store.create(_callback_dag("huge-dag"))
        await store.update_dag_status(
            dag.id, "completed", result_summary="x" * 20000
        )
        dag = await store.get_dag(dag.id)

        http = _ok_http()
        delivery = DAGResultDelivery(_settings(), agent_id="a", http=http)
        await delivery.deliver(dag)

        sent = http.post.await_args.kwargs["json"]["text"]
        assert len(sent) <= 4096


class TestAgentSummaryLeg:
    @pytest.mark.asyncio
    async def test_agent_summary_replaces_template(self, store):
        dag = await _finished_dag(store)
        runner = AsyncMock()
        runner.run_turn.return_value = ("Everything shipped cleanly.", None, {})
        http = _ok_http()

        delivery = DAGResultDelivery(
            _settings(dag_delivery_agent_summary_enabled=True),
            agent_id="a",
            runner=runner,
            http=http,
        )
        outcome = await delivery.deliver(dag)

        assert outcome.summary == "Everything shipped cleanly."
        assert http.post.await_args.kwargs["json"]["text"] == (
            "Everything shipped cleanly."
        )

    @pytest.mark.asyncio
    async def test_agent_summary_timeout_falls_back_to_template(self, store):
        """An unreachable LLM must degrade the message, not block the push."""
        dag = await _finished_dag(store)

        async def _hang(*args, **kwargs):
            await asyncio.sleep(10)

        runner = AsyncMock()
        runner.run_turn.side_effect = _hang
        http = _ok_http()

        delivery = DAGResultDelivery(
            _settings(
                dag_delivery_agent_summary_enabled=True,
                dag_delivery_agent_summary_timeout_seconds=0.05,
            ),
            agent_id="a",
            runner=runner,
            http=http,
        )
        outcome = await delivery.deliver(dag)

        assert outcome.delivered is True
        assert "delivery-dag" in outcome.summary
        leg = next(leg for leg in outcome.legs if leg.name == "summary")
        assert leg.ok is False and leg.required is False

    @pytest.mark.asyncio
    async def test_agent_summary_error_falls_back_to_template(self, store):
        dag = await _finished_dag(store)
        runner = AsyncMock()
        runner.run_turn.side_effect = RuntimeError("model unavailable")

        delivery = DAGResultDelivery(
            _settings(dag_delivery_agent_summary_enabled=True),
            agent_id="a",
            runner=runner,
            http=_ok_http(),
        )
        outcome = await delivery.deliver(dag)

        assert outcome.delivered is True
        assert "delivery-dag" in outcome.summary


class TestTemplate:
    @pytest.mark.asyncio
    async def test_template_leads_with_failures(self, store):
        dag = await store.create(_callback_dag("problem-dag"))
        await store.update_node(
            dag.nodes[0].id, status="failed", error="disk full"
        )
        await store.update_dag_status(dag.id, "failed")
        dag = await store.get_dag(dag.id)

        text = DAGResultDelivery(_settings(), agent_id="a").build_template(dag)

        assert "FAILED" in text
        assert "Problems:" in text
        assert "disk full" in text

    @pytest.mark.asyncio
    async def test_template_never_raises_on_empty_dag(self, store):
        dag = await _finished_dag(store)
        dag.nodes = []
        text = DAGResultDelivery(_settings(), agent_id="a").build_template(dag)
        assert "delivery-dag" in text


# ---------------------------------------------------------------------------
# Sweep — the durability guarantee
# ---------------------------------------------------------------------------


class TestDeliverySweep:
    def _orch(self, store, subtask_mgr, dynamic_loader, delivery, settings=None):
        orch = DAGOrchestrator(
            store=store,
            subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader,
            settings=settings or _settings(),
            delivery=delivery,
        )
        orch.clock_wired = True
        return orch

    @pytest.mark.asyncio
    async def test_tick_delivers_terminal_dag(
        self, store, subtask_mgr, dynamic_loader
    ):
        dag = await _finished_dag(store)
        delivery = AsyncMock()
        delivery.deliver.return_value = SimpleNamespace(
            delivered=True, legs=(), summary="ok"
        )
        orch = self._orch(store, subtask_mgr, dynamic_loader, delivery)

        await orch.tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.delivered_at is not None
        assert delivery.deliver.await_count == 1

    @pytest.mark.asyncio
    async def test_delivered_dag_is_not_redelivered(
        self, store, subtask_mgr, dynamic_loader
    ):
        await _finished_dag(store)
        delivery = AsyncMock()
        delivery.deliver.return_value = SimpleNamespace(
            delivered=True, legs=(), summary="ok"
        )
        orch = self._orch(store, subtask_mgr, dynamic_loader, delivery)

        await orch.tick()
        await orch.tick()

        assert delivery.deliver.await_count == 1

    @pytest.mark.asyncio
    async def test_undelivered_dag_survives_restart(
        self, store, subtask_mgr, dynamic_loader
    ):
        """The crash-resume property.

        A DAG marked terminal by a process that died before delivering is
        picked up by a FRESH orchestrator, because the work queue is a table
        rather than in-memory state.
        """
        dag = await _finished_dag(store)

        # First orchestrator dies mid-delivery — nothing marked delivered.
        dead_delivery = AsyncMock()
        dead_delivery.deliver.side_effect = RuntimeError("process died")
        dead = self._orch(store, subtask_mgr, dynamic_loader, dead_delivery)
        await dead.tick()

        assert (await store.get_dag(dag.id)).delivered_at is None

        # A brand-new orchestrator picks it back up.
        live_delivery = AsyncMock()
        live_delivery.deliver.return_value = SimpleNamespace(
            delivered=True, legs=(), summary="ok"
        )
        live = self._orch(store, subtask_mgr, dynamic_loader, live_delivery)
        await live.tick()

        assert (await store.get_dag(dag.id)).delivered_at is not None

    @pytest.mark.asyncio
    async def test_retries_are_bounded_then_give_up_visibly(
        self, store, subtask_mgr, dynamic_loader
    ):
        """A permanently-failing channel must not retry forever, and the
        reason must stay on the row rather than vanishing."""
        dag = await _finished_dag(store)
        delivery = AsyncMock()
        delivery.deliver.return_value = SimpleNamespace(
            delivered=False,
            legs=(),
            summary="ok",
            failure_detail="telegram: HTTP 500",
        )
        orch = self._orch(
            store,
            subtask_mgr,
            dynamic_loader,
            delivery,
            settings=_settings(dag_delivery_max_attempts=3),
        )

        for _ in range(5):
            await orch.tick()

        fetched = await store.get_dag(dag.id)
        assert fetched.delivered_at is not None  # gave up, stopped looping
        assert fetched.delivery_attempts == 3
        assert "gave up after 3 attempts" in fetched.delivery_error
        assert "HTTP 500" in fetched.delivery_error
        assert delivery.deliver.await_count == 3

    @pytest.mark.asyncio
    async def test_sweep_disabled_by_flag(
        self, store, subtask_mgr, dynamic_loader
    ):
        dag = await _finished_dag(store)
        delivery = AsyncMock()
        orch = self._orch(
            store,
            subtask_mgr,
            dynamic_loader,
            delivery,
            settings=_settings(dag_result_delivery_enabled=False),
        )

        await orch.tick()

        assert delivery.deliver.await_count == 0
        assert (await store.get_dag(dag.id)).delivered_at is None

    @pytest.mark.asyncio
    async def test_no_delivery_collaborator_is_safe(
        self, store, subtask_mgr, dynamic_loader
    ):
        """Orchestrator without a delivery collaborator still ticks."""
        dag = await _finished_dag(store)
        orch = DAGOrchestrator(
            store=store,
            subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader,
            settings=_settings(),
        )

        await orch.tick()

        assert (await store.get_dag(dag.id)).delivered_at is None

    @pytest.mark.asyncio
    async def test_batch_size_caps_per_tick_drain(
        self, store, subtask_mgr, dynamic_loader
    ):
        for i in range(4):
            await _finished_dag(store, f"batch-dag-{i}")
        delivery = AsyncMock()
        delivery.deliver.return_value = SimpleNamespace(
            delivered=True, legs=(), summary="ok"
        )
        orch = self._orch(
            store,
            subtask_mgr,
            dynamic_loader,
            delivery,
            settings=_settings(dag_delivery_batch_size=2),
        )

        await orch.tick()

        assert delivery.deliver.await_count == 2
