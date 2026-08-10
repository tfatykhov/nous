"""Tests for F038 DAG Store CRUD operations."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore, MAX_ACTIVE_DAGS

@pytest_asyncio.fixture
async def store(db):
    """DAGStore instance with unique agent_id per test."""
    agent_id = f"test-dag-store-{uuid.uuid4().hex[:8]}"
    return DAGStore(db, agent_id, Settings(_env_file=None))


# F046: hermetic Settings for timeout resolution tests — explicit values
# so the tests don't care about ambient NOUS_DAG_NODE_* env.
# F087: _env_file=None completes that intent. Pinning the timeouts was not
# enough — a developer .env with NOUS_DAG_STALL_DETECTION_ENABLED=true still
# leaked in and tripped store.create's stall-vs-timeout validator, failing
# these tests on the author's machine but not in CI.
_TEST_DAG_SETTINGS = Settings(
    _env_file=None,
    dag_node_default_timeout=600,
    dag_node_max_timeout=7200,
)


@pytest.fixture
def dag_settings():
    return _TEST_DAG_SETTINGS


@pytest_asyncio.fixture
async def agent_id():
    return f"test-dag-store-{uuid.uuid4().hex[:8]}"


def _simple_request(name: str = "test-dag") -> DAGCreateRequest:
    """Create a simple single-node DAG request."""
    return DAGCreateRequest(
        name=name,
        description="Test DAG",
        nodes=[
            DAGNodeSpec(name="task-1", type=DAGNodeType.subtask, instructions="Do something"),
        ],
    )


def _two_node_request(name: str = "two-node-dag") -> DAGCreateRequest:
    """Create a two-node DAG with dependency."""
    return DAGCreateRequest(
        name=name,
        description="Two-node DAG",
        nodes=[
            DAGNodeSpec(name="step-1", type=DAGNodeType.subtask, instructions="First step"),
            DAGNodeSpec(name="step-2", type=DAGNodeType.subtask, instructions="Second step"),
        ],
        edges=[
            DAGEdgeSpec(from_node="step-1", to_node="step-2", edge_type="dependency"),
        ],
    )


class TestDAGStoreCreate:
    """Test DAGStore.create()."""

    @pytest.mark.asyncio
    async def test_create_dag(self, store):
        """Create a DAG and verify nodes/edges count and wave-0 readiness."""
        request = _two_node_request()
        dag = await store.create(request)

        assert dag.id is not None
        assert dag.name == "two-node-dag"
        assert dag.status == "pending"
        assert dag.agent_id is not None
        assert len(dag.nodes) == 2
        assert len(dag.edges) == 1

        # Wave-0 node should be ready
        step1 = next(n for n in dag.nodes if n.name == "step-1")
        assert step1.status == "ready"
        assert step1.wave == 0

        # Wave-1 node should be pending
        step2 = next(n for n in dag.nodes if n.name == "step-2")
        assert step2.status == "pending"
        assert step2.wave == 1

    @pytest.mark.asyncio
    async def test_create_single_node(self, store):
        """Single node DAG — node is wave-0 and ready."""
        dag = await store.create(_simple_request())
        assert len(dag.nodes) == 1
        assert dag.nodes[0].status == "ready"
        assert dag.nodes[0].wave == 0


class TestDAGStoreGet:
    """Test DAGStore fetch operations."""

    @pytest.mark.asyncio
    async def test_get_dag(self, store):
        """Create then fetch a DAG by ID."""
        dag = await store.create(_simple_request("get-test"))
        fetched = await store.get_dag(dag.id)

        assert fetched is not None
        assert fetched.id == dag.id
        assert fetched.name == "get-test"
        assert len(fetched.nodes) == 1

    @pytest.mark.asyncio
    async def test_get_dag_not_found(self, store):
        """Non-existent DAG returns None."""
        result = await store.get_dag(uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_get_active_dags(self, store):
        """Active DAGs include pending and running."""
        dag = await store.create(_simple_request("active-test"))
        await store.update_dag_status(dag.id, "running")

        active = await store.get_active_dags()
        dag_ids = {d.id for d in active}
        assert dag.id in dag_ids


class TestDAGStoreUpdate:
    """Test DAGStore update operations."""

    @pytest.mark.asyncio
    async def test_update_node_status(self, store):
        """Update a node's status."""
        dag = await store.create(_simple_request("update-node-test"))
        node = dag.nodes[0]

        await store.update_node(node.id, status="running")
        fetched = await store.get_dag(dag.id)
        assert fetched.nodes[0].status == "running"

    @pytest.mark.asyncio
    async def test_update_dag_status_sets_started_at(self, store):
        """Setting status to running sets started_at."""
        dag = await store.create(_simple_request("started-at-test"))
        assert dag.started_at is None

        await store.update_dag_status(dag.id, "running")
        fetched = await store.get_dag(dag.id)
        assert fetched.status == "running"
        assert fetched.started_at is not None

    @pytest.mark.asyncio
    async def test_update_dag_status_sets_completed_at(self, store):
        """Setting status to completed sets completed_at."""
        dag = await store.create(_simple_request("completed-at-test"))
        await store.update_dag_status(dag.id, "running")
        await store.update_dag_status(dag.id, "completed", result_summary="Done")

        fetched = await store.get_dag(dag.id)
        assert fetched.status == "completed"
        assert fetched.completed_at is not None
        assert fetched.result_summary == "Done"

    @pytest.mark.asyncio
    async def test_update_dag_tokens(self, store):
        """Incrementing tokens_consumed works."""
        dag = await store.create(_simple_request("tokens-test"))
        await store.update_dag_tokens(dag.id, 100)
        await store.update_dag_tokens(dag.id, 50)

        fetched = await store.get_dag(dag.id)
        assert fetched.tokens_consumed == 150

    @pytest.mark.asyncio
    async def test_count_active(self, store):
        """count_active returns correct count."""
        initial = await store.count_active()
        await store.create(_simple_request("count-1"))
        await store.create(_simple_request("count-2"))
        assert await store.count_active() == initial + 2


class TestDAGStoreMaxActive:
    """Test active DAG limit enforcement."""

    @pytest.mark.asyncio
    async def test_max_active_dags_enforced(self, store):
        """Creating more than MAX_ACTIVE_DAGS raises ValueError."""
        # Create MAX_ACTIVE_DAGS pending DAGs
        for i in range(MAX_ACTIVE_DAGS):
            await store.create(_simple_request(f"max-test-{i}"))

        # The next one should fail
        with pytest.raises(ValueError, match="Active DAG limit reached"):
            await store.create(_simple_request("one-too-many"))


class TestDAGStoreIsolation:
    """Test agent_id isolation."""

    @pytest.mark.asyncio
    async def test_update_node_cross_agent_rejected(self, db):
        """update_node cannot modify nodes belonging to another agent's DAG."""
        store_a = DAGStore(db, f"agent-a-{uuid.uuid4().hex[:8]}", Settings(_env_file=None))
        store_b = DAGStore(db, f"agent-b-{uuid.uuid4().hex[:8]}", Settings(_env_file=None))

        dag = await store_a.create(_simple_request("isolation-test"))
        node_id = dag.nodes[0].id

        # Agent B tries to update Agent A's node
        await store_b.update_node(node_id, status="failed", error="hijacked")

        # Should not have changed
        fetched = await store_a.get_dag(dag.id)
        assert fetched.nodes[0].status == "ready"  # Unchanged
        assert fetched.nodes[0].error is None

    @pytest.mark.asyncio
    async def test_get_dag_cross_agent_rejected(self, db):
        """get_dag returns None for another agent's DAG."""
        store_a = DAGStore(db, f"agent-a-{uuid.uuid4().hex[:8]}", Settings(_env_file=None))
        store_b = DAGStore(db, f"agent-b-{uuid.uuid4().hex[:8]}", Settings(_env_file=None))

        dag = await store_a.create(_simple_request("cross-agent-test"))

        fetched = await store_b.get_dag(dag.id)
        assert fetched is None


class TestDAGStoreTimeoutResolution:
    """F046: Store resolves None→default and clamps to max at insert."""

    @pytest.mark.asyncio
    async def test_create_resolves_none_to_default(self, db, agent_id, dag_settings):
        store = DAGStore(db, agent_id, dag_settings)
        req = DAGCreateRequest(
            name="resolve-none",
            nodes=[DAGNodeSpec(name="n", type=DAGNodeType.subtask, instructions="x")],
        )
        dag = await store.create(req)
        assert dag.nodes[0].timeout_seconds == dag_settings.dag_node_default_timeout

    @pytest.mark.asyncio
    async def test_create_clamps_to_max(self, db, agent_id, dag_settings):
        store = DAGStore(db, agent_id, dag_settings)
        req = DAGCreateRequest(
            name="clamp-max",
            nodes=[
                DAGNodeSpec(
                    name="n",
                    type=DAGNodeType.subtask,
                    instructions="x",
                    timeout_seconds=999999,
                )
            ],
        )
        dag = await store.create(req)
        assert dag.nodes[0].timeout_seconds == dag_settings.dag_node_max_timeout

    @pytest.mark.asyncio
    async def test_create_preserves_explicit_value(self, db, agent_id, dag_settings):
        store = DAGStore(db, agent_id, dag_settings)
        req = DAGCreateRequest(
            name="preserve-explicit",
            nodes=[
                DAGNodeSpec(
                    name="n",
                    type=DAGNodeType.subtask,
                    instructions="x",
                    timeout_seconds=300,
                )
            ],
        )
        dag = await store.create(req)
        assert dag.nodes[0].timeout_seconds == 300
