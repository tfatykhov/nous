"""Tests for F064.2 — per-frame-type DAG dispatch concurrency caps.

Covers plan §5.5 acceptance criteria + the post-review revisions
(silent-failure P1-6: per-node try/except; conventions P2-6: accumulator
on success only).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore
from nous.storage.models import ExecutionDAG


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings_caps_on(**overrides) -> Settings:
    base = dict(
        dag_frame_concurrency_enabled=True,
        dag_global_max_concurrent_by_frame={},
    )
    base.update(overrides)
    return Settings(**base)


def _settings_caps_off() -> Settings:
    return Settings(dag_frame_concurrency_enabled=False)


@pytest_asyncio.fixture
async def store_caps_on(db):
    return DAGStore(db, f"test-caps-{uuid.uuid4().hex[:8]}", _settings_caps_on())


@pytest_asyncio.fixture
async def store_caps_off(db):
    return DAGStore(db, f"test-caps-off-{uuid.uuid4().hex[:8]}", _settings_caps_off())


@pytest.fixture
def subtask_mgr_running():
    """Mock subtask manager that returns a 'running' subtask after launch."""
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    mgr.get.return_value = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        result=None,
        error=None,
        final_outcome=None,
        report_jsonb=None,
    )
    return mgr


@pytest.fixture
def dynamic_loader():
    loader = AsyncMock()
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


def _orch(store, subtask_mgr, dynamic_loader, settings):
    return DAGOrchestrator(
        store=store,
        subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader,
        settings=settings,
    )


def _multi_frame_dag(
    frames_count: dict[str, int],
    caps: dict[str, int] | None = None,
) -> DAGCreateRequest:
    """Build a wave-0 DAG with N nodes per requested frame type."""
    nodes: list[DAGNodeSpec] = []
    for frame, count in frames_count.items():
        for i in range(count):
            nodes.append(
                DAGNodeSpec(
                    name=f"{frame}-{i}",
                    type=DAGNodeType.subtask,
                    instructions=f"{frame} work {i}",
                    frame_type=frame if frame != "_default" else None,
                )
            )
    return DAGCreateRequest(
        name="caps-test-dag",
        nodes=nodes,
        max_concurrent_by_frame_type=caps,
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class TestValidator:
    def test_validator_rejects_zero_cap(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            DAGCreateRequest(
                name="bad",
                nodes=[DAGNodeSpec(name="x", type=DAGNodeType.subtask)],
                max_concurrent_by_frame_type={"debug": 0},
            )

    def test_validator_rejects_negative_cap(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            DAGCreateRequest(
                name="bad",
                nodes=[DAGNodeSpec(name="x", type=DAGNodeType.subtask)],
                max_concurrent_by_frame_type={"debug": -1},
            )

    def test_validator_accepts_none(self):
        req = DAGCreateRequest(
            name="ok",
            nodes=[DAGNodeSpec(name="x", type=DAGNodeType.subtask)],
            max_concurrent_by_frame_type=None,
        )
        assert req.max_concurrent_by_frame_type is None

    def test_validator_accepts_positive_caps(self):
        req = DAGCreateRequest(
            name="ok",
            nodes=[DAGNodeSpec(name="x", type=DAGNodeType.subtask)],
            max_concurrent_by_frame_type={"debug": 1, "research": 3},
        )
        assert req.max_concurrent_by_frame_type == {"debug": 1, "research": 3}

    def test_settings_field_validator_rejects_zero_in_env(self):
        """Settings.dag_global_max_concurrent_by_frame field_validator."""
        with pytest.raises(ValueError, match="must be >= 1"):
            Settings(dag_global_max_concurrent_by_frame={"debug": 0})


# ---------------------------------------------------------------------------
# Store persistence
# ---------------------------------------------------------------------------


class TestStorePersistence:
    @pytest.mark.asyncio
    async def test_store_persists_max_concurrent_field(self, store_caps_on):
        req = _multi_frame_dag(
            frames_count={"debug": 1, "research": 1},
            caps={"debug": 1, "research": 3},
        )
        dag = await store_caps_on.create(req)
        fetched = await store_caps_on.get_dag(dag.id)
        assert fetched.max_concurrent_by_frame_type == {"debug": 1, "research": 3}

    @pytest.mark.asyncio
    async def test_store_preserves_none_max_concurrent(self, store_caps_on):
        req = _multi_frame_dag(frames_count={"debug": 1}, caps=None)
        dag = await store_caps_on.create(req)
        fetched = await store_caps_on.get_dag(dag.id)
        assert fetched.max_concurrent_by_frame_type is None

    @pytest.mark.asyncio
    async def test_count_running_by_frame_type_groups_correctly(
        self, store_caps_on, db
    ):
        """count_running_subtasks_by_frame_type groups by frame_type for
        in-flight (pending + running) rows scoped to agent_id. Completed and
        failed rows are excluded; @codex P1 on c3a4fed required including
        pending so a just-dispatched-but-not-yet-dequeued subtask remains
        visible to the cap check on the subsequent tick."""
        from datetime import UTC, datetime

        from nous.storage.models import Subtask

        async with db.session() as session:
            # Mix of running and pending — both should be counted.
            for status, frame in [
                ("running", "debug"),
                ("pending", "debug"),  # @codex P1: pending must count too
                ("running", "research"),
                ("pending", None),
            ]:
                session.add(
                    Subtask(
                        agent_id=store_caps_on._agent_id,
                        task=f"task {status}/{frame}",
                        status=status,
                        frame_type=frame,
                        created_at=datetime.now(UTC),
                    )
                )
            # Terminal rows must NOT count.
            for status, frame in [
                ("completed", "debug"),
                ("failed", "research"),
                ("cancelled", "debug"),
            ]:
                session.add(
                    Subtask(
                        agent_id=store_caps_on._agent_id,
                        task=f"terminal {status}",
                        status=status,
                        frame_type=frame,
                        created_at=datetime.now(UTC),
                    )
                )
            await session.commit()

        counts = await store_caps_on.count_running_subtasks_by_frame_type()
        assert counts == {"debug": 2, "research": 1, "_default": 1}


# ---------------------------------------------------------------------------
# Dispatch gating
# ---------------------------------------------------------------------------


class TestDispatchGating:
    @pytest.mark.asyncio
    async def test_disabled_flag_dispatches_all_ready_nodes(
        self, store_caps_off, subtask_mgr_running, dynamic_loader
    ):
        """Flag off → legacy behavior (every ready node launched in one tick)."""
        orchestrator = _orch(
            store_caps_off, subtask_mgr_running, dynamic_loader, _settings_caps_off()
        )
        # Cap dict is set but flag-off path should ignore it entirely.
        req = _multi_frame_dag(
            frames_count={"debug": 3},
            caps={"debug": 1},
        )
        dag = await store_caps_off.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_caps_off.get_dag(dag.id)
        # All 3 debug nodes should be running (legacy: dispatch ignores caps)
        assert sum(1 for n in fetched.nodes if n.status == "running") == 3

    @pytest.mark.asyncio
    async def test_per_dag_cap_blocks_excess(
        self, store_caps_on, subtask_mgr_running, dynamic_loader
    ):
        """Cap of 1 on debug → only 1 of 3 debug nodes launches; rest stay pending."""
        orchestrator = _orch(
            store_caps_on, subtask_mgr_running, dynamic_loader, _settings_caps_on()
        )
        req = _multi_frame_dag(
            frames_count={"debug": 3},
            caps={"debug": 1},
        )
        dag = await store_caps_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_caps_on.get_dag(dag.id)
        running = [n for n in fetched.nodes if n.status == "running"]
        pending = [n for n in fetched.nodes if n.status in ("pending", "ready")]
        assert len(running) == 1
        assert len(pending) == 2

    @pytest.mark.asyncio
    async def test_in_memory_accumulator_prevents_overdispatch_within_tick(
        self, store_caps_on, subtask_mgr_running, dynamic_loader
    ):
        """4 ready nodes of same frame, cap=2 → exactly 2 launch in one tick.

        The DB count_running_subtasks_by_frame_type doesn't refresh between
        per-node awaits inside the tick; the in-memory accumulator is what
        prevents launching all 4. (4 is the MAX_PARALLEL_PER_WAVE limit.)
        """
        orchestrator = _orch(
            store_caps_on, subtask_mgr_running, dynamic_loader, _settings_caps_on()
        )
        req = _multi_frame_dag(
            frames_count={"research": 4},
            caps={"research": 2},
        )
        dag = await store_caps_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_caps_on.get_dag(dag.id)
        assert sum(1 for n in fetched.nodes if n.status == "running") == 2

    @pytest.mark.asyncio
    async def test_unmapped_frame_is_uncapped(
        self, store_caps_on, subtask_mgr_running, dynamic_loader
    ):
        """Cap dict {debug: 1} doesn't restrict 'analysis' — all analysis nodes launch."""
        orchestrator = _orch(
            store_caps_on, subtask_mgr_running, dynamic_loader, _settings_caps_on()
        )
        req = _multi_frame_dag(
            frames_count={"analysis": 4},
            caps={"debug": 1},  # no key for analysis
        )
        dag = await store_caps_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_caps_on.get_dag(dag.id)
        assert sum(1 for n in fetched.nodes if n.status == "running") == 4

    @pytest.mark.asyncio
    async def test_env_override_wins_over_per_dag(
        self, store_caps_on, subtask_mgr_running, dynamic_loader, db
    ):
        """Env-level cap on debug=1 trumps per-DAG debug=3."""
        # Need to rebuild store + orchestrator with env override set.
        s = _settings_caps_on(dag_global_max_concurrent_by_frame={"debug": 1})
        store = DAGStore(db, f"test-override-{uuid.uuid4().hex[:8]}", s)
        orchestrator = _orch(store, subtask_mgr_running, dynamic_loader, s)
        req = _multi_frame_dag(
            frames_count={"debug": 3},
            caps={"debug": 3},  # per-DAG generous; env should clamp to 1
        )
        dag = await store.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        assert sum(1 for n in fetched.nodes if n.status == "running") == 1

    @pytest.mark.asyncio
    async def test_launch_failure_does_not_increment_accumulator(
        self, store_caps_on, subtask_mgr_running, dynamic_loader
    ):
        """If _launch_node raises, the slot isn't consumed — next iteration in
        the same tick should be able to use it (silent-failure P1-6 fix).
        """
        orchestrator = _orch(
            store_caps_on, subtask_mgr_running, dynamic_loader, _settings_caps_on()
        )
        # First create call raises; second succeeds.
        original_create = subtask_mgr_running.create
        call_count = {"n": 0}

        async def _flaky_create(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated launch failure")
            return await original_create(*args, **kwargs)

        subtask_mgr_running.create = _flaky_create

        req = _multi_frame_dag(
            frames_count={"debug": 3},
            caps={"debug": 2},  # 2 slots
        )
        dag = await store_caps_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_caps_on.get_dag(dag.id)
        # First node fails its launch → goes 'failed'. Second + third should
        # both be 'running' (cap=2, accumulator still has 2 slots open after
        # the failure).
        failed = [n for n in fetched.nodes if n.status == "failed"]
        running = [n for n in fetched.nodes if n.status == "running"]
        assert len(failed) == 1
        assert len(running) == 2

    @pytest.mark.asyncio
    async def test_deferred_wave_zero_node_relaunches_next_tick(
        self, store_caps_on, dynamic_loader, db
    ):
        """@codex P1 on ab02178: wave-0 nodes arrive at dispatch in status='ready'.
        If deferred, they MUST be requeued to 'pending' so _find_ready_nodes
        picks them up on the next tick. Otherwise they're stuck in 'ready'
        forever and the cap silently starves the bucket.
        """
        from datetime import UTC, datetime

        from nous.storage.models import Subtask

        # Use a fresh subtask manager whose subtasks transition completed
        # so the deferred node can re-launch on tick 2.
        completed_subtasks: dict = {}
        subtask_mgr = AsyncMock()

        async def _create(*args, **kwargs):
            sid = uuid.uuid4()
            completed_subtasks[sid] = "running"
            return SimpleNamespace(id=sid, status="pending")

        async def _get(subtask_id):
            return SimpleNamespace(
                id=subtask_id,
                status=completed_subtasks.get(subtask_id, "running"),
                result="ok",
                error=None,
                final_outcome=None,
                report_jsonb=None,
            )

        subtask_mgr.create = _create
        subtask_mgr.get = _get

        orchestrator = _orch(
            store_caps_on, subtask_mgr, dynamic_loader, _settings_caps_on()
        )
        req = _multi_frame_dag(
            frames_count={"debug": 2},
            caps={"debug": 1},
        )
        dag = await store_caps_on.create(req)
        await orchestrator.start_dag(dag.id)

        # After start_dag: 1 running, 1 deferred (must be pending, not ready)
        fetched = await store_caps_on.get_dag(dag.id)
        running_nodes = [n for n in fetched.nodes if n.status == "running"]
        deferred_nodes = [n for n in fetched.nodes if n.status == "pending"]
        assert len(running_nodes) == 1
        assert len(deferred_nodes) == 1

        # Complete the running subtask so its slot frees up.
        first_subtask_id = running_nodes[0].subtask_id
        completed_subtasks[first_subtask_id] = "completed"

        # Tick again — deferred wave-0 node should now run.
        await orchestrator.tick()
        fetched = await store_caps_on.get_dag(dag.id)
        # First node completed; second node is now running.
        statuses = sorted(n.status for n in fetched.nodes)
        assert statuses == ["completed", "running"]

    @pytest.mark.asyncio
    async def test_acceptance_scenario(
        self, store_caps_on, subtask_mgr_running, dynamic_loader
    ):
        """Spec §F064.2 acceptance (adapted to MAX_PARALLEL_PER_WAVE=4):
        caps={debug:1, research:2}, 1 debug + 3 research nodes → first tick
        runs 1 debug + 2 research concurrently; 3rd research waits.

        Same demonstration as the original spec wording (1 + 4 with cap 3),
        scaled to fit the existing 4-parallel-per-wave constraint.
        """
        orchestrator = _orch(
            store_caps_on, subtask_mgr_running, dynamic_loader, _settings_caps_on()
        )
        req = _multi_frame_dag(
            frames_count={"debug": 1, "research": 3},
            caps={"debug": 1, "research": 2},
        )
        dag = await store_caps_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_caps_on.get_dag(dag.id)
        running = [n for n in fetched.nodes if n.status == "running"]
        pending = [n for n in fetched.nodes if n.status in ("pending", "ready")]
        running_by_frame = {f: 0 for f in ["debug", "research"]}
        for n in running:
            if n.frame_type:
                running_by_frame[n.frame_type] += 1
        assert running_by_frame == {"debug": 1, "research": 2}
        assert len(pending) == 1
        assert pending[0].frame_type == "research"
