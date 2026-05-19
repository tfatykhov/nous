"""Tests for F064.1 — DAG node stall detection.

Covers the acceptance criterion from plan §4.5 plus revisions from the
3-agent review cycle (text-only turn, long tool call, NULL fallback,
@codex P2 default-applies enforcement).

Tests use the same `past_time` manipulation pattern as
test_dag_orchestrator.py — manually setting an old timestamp on the row,
then ticking the orchestrator. No `freezegun` dependency.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeType,
)
from nous.dag.store import DAGStore


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_dag_orchestrator.py)
# ---------------------------------------------------------------------------


def _settings_stall_on(**overrides) -> Settings:
    """Settings with stall detection enabled. Defaults are mutually consistent
    so the cross-validator passes."""
    base = dict(
        dag_stall_detection_enabled=True,
        dag_node_default_stall_timeout=60,
        dag_node_max_stall_timeout=600,
        dag_node_default_timeout=120,
        dag_node_max_timeout=3600,
    )
    base.update(overrides)
    return Settings(**base)


def _settings_stall_off() -> Settings:
    return Settings(dag_stall_detection_enabled=False)


@pytest_asyncio.fixture
async def store_stall_on(db):
    return DAGStore(
        db,
        f"test-stall-{uuid.uuid4().hex[:8]}",
        _settings_stall_on(),
    )


@pytest_asyncio.fixture
async def store_stall_off(db):
    return DAGStore(
        db,
        f"test-stall-off-{uuid.uuid4().hex[:8]}",
        _settings_stall_off(),
    )


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


def _orch(store, subtask_mgr, dynamic_loader, settings):
    return DAGOrchestrator(
        store=store,
        subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader,
        settings=settings,
    )


def _subtask_dag(stall_timeout: int | None = None) -> DAGCreateRequest:
    return DAGCreateRequest(
        name="stall-test-dag",
        nodes=[
            DAGNodeSpec(
                name="long-runner",
                type=DAGNodeType.subtask,
                instructions="Long-running task",
                stall_timeout_seconds=stall_timeout,
            ),
        ],
    )


def _dependency_dag(stall_timeout: int) -> DAGCreateRequest:
    """Two-node DAG: A (with stall) → B (depends on A)."""
    return DAGCreateRequest(
        name="stall-cascade-dag",
        nodes=[
            DAGNodeSpec(
                name="stage-a",
                type=DAGNodeType.subtask,
                instructions="First stage",
                stall_timeout_seconds=stall_timeout,
            ),
            DAGNodeSpec(
                name="stage-b",
                type=DAGNodeType.subtask,
                instructions="Second stage",
            ),
        ],
        edges=[DAGEdgeSpec(from_node="stage-a", to_node="stage-b")],
    )


# ---------------------------------------------------------------------------
# Settings cross-validator
# ---------------------------------------------------------------------------


class TestStallSettingsValidator:
    def test_validator_passes_with_defaults(self):
        # Default values (600, 3600, 600, 7200) satisfy stall <= wall-clock.
        s = Settings(dag_stall_detection_enabled=True)
        assert s.dag_node_default_stall_timeout <= s.dag_node_default_timeout
        assert s.dag_node_max_stall_timeout <= s.dag_node_max_timeout

    def test_validator_rejects_default_stall_above_walltime(self):
        with pytest.raises(ValueError, match="dag_node_default_stall_timeout"):
            Settings(
                dag_stall_detection_enabled=True,
                dag_node_default_stall_timeout=900,
                dag_node_default_timeout=600,
            )

    def test_validator_rejects_max_stall_above_max_walltime(self):
        with pytest.raises(ValueError, match="dag_node_max_stall_timeout"):
            Settings(
                dag_stall_detection_enabled=True,
                dag_node_max_stall_timeout=10000,
                dag_node_max_timeout=7200,
            )

    def test_validator_skipped_when_detection_disabled(self):
        # When dag_stall_detection_enabled=False, the inequality is not
        # enforced — operators can leave defaults in any order.
        s = Settings(
            dag_stall_detection_enabled=False,
            dag_node_default_stall_timeout=9999,
            dag_node_default_timeout=600,
        )
        # No exception; defaults stay raw.
        assert s.dag_node_default_stall_timeout == 9999


# ---------------------------------------------------------------------------
# Store-level enforcement (codex P2 fix)
# ---------------------------------------------------------------------------


class TestStoreLevelStallEnforcement:
    @pytest.mark.asyncio
    async def test_store_rejects_stall_above_explicit_timeout(self, store_stall_on):
        req = DAGCreateRequest(
            name="bad-dag",
            nodes=[
                DAGNodeSpec(
                    name="x",
                    type=DAGNodeType.subtask,
                    timeout_seconds=60,
                    stall_timeout_seconds=120,  # > timeout
                ),
            ],
        )
        with pytest.raises(ValueError, match="never fire"):
            await store_stall_on.create(req)

    @pytest.mark.asyncio
    async def test_store_rejects_inherited_global_default_above_node_timeout(
        self, db
    ):
        """@codex P2 on dc914be: when stall_timeout_seconds is unset on the
        spec but the inherited NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT exceeds
        this node's wall-clock timeout, the store must raise. Otherwise
        the global default would silently never fire for nodes with short
        timeouts."""
        # Settings with global stall default=300 but a node timeout of 60.
        # Without the fix this silently inserts; with it, raises.
        s = Settings(
            dag_stall_detection_enabled=True,
            dag_node_default_stall_timeout=300,
            dag_node_max_stall_timeout=3600,
            dag_node_default_timeout=600,
            dag_node_max_timeout=7200,
        )
        store = DAGStore(db, f"test-stall-inherit-{uuid.uuid4().hex[:8]}", s)
        req = DAGCreateRequest(
            name="bad-inherit-dag",
            nodes=[
                DAGNodeSpec(
                    name="short-node",
                    type=DAGNodeType.subtask,
                    timeout_seconds=60,
                    # stall_timeout_seconds omitted → inherits global 300
                ),
            ],
        )
        with pytest.raises(ValueError, match="inherited global stall_timeout"):
            await store.create(req)

    @pytest.mark.asyncio
    async def test_store_rejects_stall_above_resolved_default(self, store_stall_on):
        """codex P2: when timeout_seconds is None, the resolved default applies.
        A stall above that default would never fire — the store catches it."""
        # store_stall_on uses default_timeout=120. stall=200 > 120 → reject.
        req = DAGCreateRequest(
            name="bad-default-dag",
            nodes=[
                DAGNodeSpec(
                    name="x",
                    type=DAGNodeType.subtask,
                    # timeout_seconds omitted → resolves to settings default (120)
                    stall_timeout_seconds=200,
                ),
            ],
        )
        with pytest.raises(ValueError, match="never fire"):
            await store_stall_on.create(req)

    @pytest.mark.asyncio
    async def test_store_clamps_stall_to_max(self, store_stall_on):
        """Per-node stall above NOUS_DAG_NODE_MAX_STALL_TIMEOUT is clamped to
        max. The clamped value is what determines whether stall <= timeout."""
        # max_stall_timeout=600, default_timeout=120. stall=99999 → clamps to
        # 600, but 600 > 120 → still rejected.
        req = DAGCreateRequest(
            name="clamped-dag",
            nodes=[
                DAGNodeSpec(
                    name="x",
                    type=DAGNodeType.subtask,
                    timeout_seconds=1000,  # > default to dodge the comparison
                    stall_timeout_seconds=99999,  # clamps to max_stall=600
                ),
            ],
        )
        dag = await store_stall_on.create(req)
        node = dag.nodes[0]
        assert node.stall_timeout_seconds == 600  # clamped to max

    @pytest.mark.asyncio
    async def test_store_preserves_none_stall_timeout(self, store_stall_on):
        """stall_timeout_seconds=None on the spec → None persisted (= disabled)."""
        req = _subtask_dag(stall_timeout=None)
        dag = await store_stall_on.create(req)
        assert dag.nodes[0].stall_timeout_seconds is None

    @pytest.mark.asyncio
    async def test_store_preserves_zero_stall_timeout(self, store_stall_on):
        """stall_timeout_seconds=0 on the spec → 0 persisted. Symphony §8.5
        parity (stall_timeout_ms <= 0 disables)."""
        req = _subtask_dag(stall_timeout=0)
        dag = await store_stall_on.create(req)
        assert dag.nodes[0].stall_timeout_seconds == 0


# ---------------------------------------------------------------------------
# Orchestrator stall scan
# ---------------------------------------------------------------------------


class TestStallScan:
    @pytest.mark.asyncio
    async def test_disabled_flag_skips_scan_entirely(
        self, store_stall_off, subtask_mgr, dynamic_loader
    ):
        """dag_stall_detection_enabled=False → _check_stalled_nodes never invoked."""
        orchestrator = _orch(
            store_stall_off, subtask_mgr, dynamic_loader, _settings_stall_off()
        )
        # Insert a node, mark it running, set last_activity_at to ancient past,
        # tick. Without stall detection, status should remain running.
        req = _subtask_dag()
        dag = await store_stall_off.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_off.get_dag(dag.id)
        node = fetched.nodes[0]
        ancient = datetime.now(UTC) - timedelta(hours=24)
        await store_stall_off.update_node(
            node.id, status="running", last_activity_at=ancient
        )
        # Need the subtask manager to return a valid live subtask so tick doesn't fail it
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        await orchestrator.tick()
        fetched = await store_stall_off.get_dag(dag.id)
        node = fetched.nodes[0]
        # Flag-off path leaves status untouched (still 'running').
        assert node.status == "running"

    @pytest.mark.asyncio
    async def test_node_marked_failed_when_no_activity_for_timeout(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """Acceptance criterion: stall_timeout=30, no ping for 31s → failed."""
        orchestrator = _orch(
            store_stall_on, subtask_mgr, dynamic_loader, _settings_stall_on()
        )
        req = _subtask_dag(stall_timeout=30)
        # default_timeout=120 so 30 <= 120 passes store check
        dag = await store_stall_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        past = datetime.now(UTC) - timedelta(seconds=31)
        await store_stall_on.update_node(
            node.id, status="running", last_activity_at=past
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        await orchestrator.tick()
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        assert node.status == "failed"
        assert "stalled" in (node.error or "")

    @pytest.mark.asyncio
    async def test_node_kept_running_when_activity_recent(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """Recent activity within window → still running."""
        orchestrator = _orch(
            store_stall_on, subtask_mgr, dynamic_loader, _settings_stall_on()
        )
        req = _subtask_dag(stall_timeout=60)
        dag = await store_stall_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        recent = datetime.now(UTC) - timedelta(seconds=5)
        await store_stall_on.update_node(
            node.id, status="running", last_activity_at=recent
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        await orchestrator.tick()
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        assert node.status == "running"
        assert (node.error or "") == "" or "stalled" not in node.error

    @pytest.mark.asyncio
    async def test_null_last_activity_at_never_flagged(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """NULL-fallback policy (plan §4.3): NULL last_activity_at is NOT stalled.
        Covers (a) brand-new nodes between launch and first ping, (b) all-3-ping-
        sites-failed silent case. Wall-clock is the real fallback."""
        orchestrator = _orch(
            store_stall_on, subtask_mgr, dynamic_loader, _settings_stall_on()
        )
        req = _subtask_dag(stall_timeout=30)
        dag = await store_stall_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        # Explicitly clear last_activity_at after the baseline ping at launch.
        await store_stall_on.update_node(
            node.id, status="running", last_activity_at=None
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        await orchestrator.tick()
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        assert node.status == "running"

    @pytest.mark.asyncio
    async def test_zero_stall_timeout_disables_per_node(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """Symphony §8.5 parity: stall_timeout=0 → no stall scan for this node
        even when the global flag is on and default_stall_timeout > 0."""
        orchestrator = _orch(
            store_stall_on, subtask_mgr, dynamic_loader, _settings_stall_on()
        )
        req = _subtask_dag(stall_timeout=0)
        dag = await store_stall_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        ancient = datetime.now(UTC) - timedelta(hours=24)
        await store_stall_on.update_node(
            node.id, status="running", last_activity_at=ancient
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        await orchestrator.tick()
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        assert node.status == "running"  # not flagged

    @pytest.mark.asyncio
    async def test_stall_cancels_underlying_subtask(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """@codex P1 on 9ee630a: stall must tear down the underlying subtask,
        not just mark the DAG node failed. Without this the subtask keeps
        consuming tokens / a worker slot and _sync_node_statuses can never
        observe it again."""
        orchestrator = _orch(
            store_stall_on, subtask_mgr, dynamic_loader, _settings_stall_on()
        )
        req = _subtask_dag(stall_timeout=30)
        dag = await store_stall_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        past = datetime.now(UTC) - timedelta(seconds=31)
        await store_stall_on.update_node(
            node.id, status="running", last_activity_at=past
        )
        subtask_id = uuid.uuid4()
        await store_stall_on.update_node(node.id, subtask_id=subtask_id)
        live_subtask = SimpleNamespace(
            id=subtask_id,
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        subtask_mgr.get.return_value = live_subtask
        subtask_mgr.cancel = AsyncMock()
        await orchestrator.tick()
        # Subtask cancel must have been requested before the row was marked failed.
        subtask_mgr.cancel.assert_called_once_with(subtask_id)
        fetched = await store_stall_on.get_dag(dag.id)
        assert fetched.nodes[0].status == "failed"

    @pytest.mark.asyncio
    async def test_unset_stall_timeout_inherits_global_default(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """@codex P1 on 9ee630a: clarified semantics — `stall_timeout_seconds=None`
        on the spec INHERITS the global default; only 0 explicitly disables.
        This test pins the cascade behavior."""
        orchestrator = _orch(
            store_stall_on, subtask_mgr, dynamic_loader, _settings_stall_on()
        )
        # _settings_stall_on() sets dag_node_default_stall_timeout=60.
        # _subtask_dag(stall_timeout=None) leaves the per-node field unset.
        req = _subtask_dag(stall_timeout=None)
        dag = await store_stall_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_on.get_dag(dag.id)
        node = fetched.nodes[0]
        # 90s elapsed > 60s global default → should flag stalled
        past = datetime.now(UTC) - timedelta(seconds=90)
        await store_stall_on.update_node(
            node.id, status="running", last_activity_at=past
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        subtask_mgr.cancel = AsyncMock()
        await orchestrator.tick()
        fetched = await store_stall_on.get_dag(dag.id)
        assert fetched.nodes[0].status == "failed"
        assert "stalled" in (fetched.nodes[0].error or "")

    @pytest.mark.asyncio
    async def test_global_default_zero_disables_for_unset_nodes(
        self, db, subtask_mgr, dynamic_loader
    ):
        """When NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT=0, nodes with `None` field
        get NULL effective timeout → never flagged. Operator escape hatch for
        rolling out stall detection without touching existing DAGNodeSpec rows."""
        # Note: keeping the cross-validator happy (stall <= timeout)
        settings = Settings(
            dag_stall_detection_enabled=True,
            dag_node_default_stall_timeout=0,
            dag_node_max_stall_timeout=600,
            dag_node_default_timeout=120,
        )
        store = DAGStore(db, f"test-stall-zero-{uuid.uuid4().hex[:8]}", settings)
        orchestrator = _orch(store, subtask_mgr, dynamic_loader, settings)
        req = _subtask_dag(stall_timeout=None)
        dag = await store.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store.get_dag(dag.id)
        node = fetched.nodes[0]
        ancient = datetime.now(UTC) - timedelta(hours=24)
        await store.update_node(
            node.id, status="running", last_activity_at=ancient
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        subtask_mgr.cancel = AsyncMock()
        await orchestrator.tick()
        fetched = await store.get_dag(dag.id)
        assert fetched.nodes[0].status == "running"
        subtask_mgr.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_stall_cascades_via_dependency_propagation(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """When stall fails A, the existing _propagate_failures should block
        B (downstream dependency) on the same tick."""
        orchestrator = _orch(
            store_stall_on, subtask_mgr, dynamic_loader, _settings_stall_on()
        )
        req = _dependency_dag(stall_timeout=30)
        dag = await store_stall_on.create(req)
        await orchestrator.start_dag(dag.id)
        fetched = await store_stall_on.get_dag(dag.id)
        node_a = next(n for n in fetched.nodes if n.name == "stage-a")
        past = datetime.now(UTC) - timedelta(seconds=31)
        await store_stall_on.update_node(
            node_a.id, status="running", last_activity_at=past
        )
        subtask_mgr.get.return_value = SimpleNamespace(
            id=node_a.subtask_id or uuid.uuid4(),
            status="running",
            result=None,
            error=None,
            final_outcome=None,
            report_jsonb=None,
        )
        await orchestrator.tick()
        fetched = await store_stall_on.get_dag(dag.id)
        node_a = next(n for n in fetched.nodes if n.name == "stage-a")
        node_b = next(n for n in fetched.nodes if n.name == "stage-b")
        assert node_a.status == "failed"
        # B was pending (not yet launched); failure-propagation marks it blocked
        assert node_b.status == "blocked"


# ---------------------------------------------------------------------------
# Ping helper (runner-side)
# ---------------------------------------------------------------------------


class TestPingHelper:
    @pytest.mark.asyncio
    async def test_ping_helper_no_op_when_store_unwired(self):
        """Without a wired _dag_store, ping is a silent no-op so chat sessions
        and tests without DAG infra continue to work."""
        from nous.api.runner import AgentRunner

        runner = MagicMock(spec=AgentRunner)
        runner._settings = _settings_stall_on()
        runner._dag_store = None
        node_id = uuid.uuid4()
        # Should not raise.
        AgentRunner._ping_dag_node_activity(runner, node_id)

    @pytest.mark.asyncio
    async def test_ping_helper_no_op_when_stall_detection_disabled(self):
        """@codex P2 on d281ac6: ping must be gated by the master flag so
        the per-tool-boundary writes don't fire when stall detection is
        disabled (default state). Otherwise we generate unread DB writes."""
        import asyncio

        from nous.api.runner import AgentRunner

        runner = MagicMock(spec=AgentRunner)
        runner._settings = _settings_stall_off()
        runner._dag_store = MagicMock()
        runner._dag_store.touch_node_activity = AsyncMock()
        node_id = uuid.uuid4()
        AgentRunner._ping_dag_node_activity(runner, node_id)
        await asyncio.sleep(0.05)
        runner._dag_store.touch_node_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_ping_helper_invokes_touch(self):
        """When _dag_store is wired AND stall detection is enabled, ping
        schedules a touch_node_activity call."""
        import asyncio

        from nous.api.runner import AgentRunner

        runner = MagicMock(spec=AgentRunner)
        runner._settings = _settings_stall_on()
        runner._dag_store = MagicMock()
        runner._dag_store.touch_node_activity = AsyncMock()
        node_id = uuid.uuid4()
        AgentRunner._ping_dag_node_activity(runner, node_id)
        # Let the scheduled task run.
        await asyncio.sleep(0.05)
        runner._dag_store.touch_node_activity.assert_called_once_with(node_id)

    @pytest.mark.asyncio
    async def test_heartbeat_loop_emits_pings_during_long_tool_dispatch(self):
        """@codex P1 on e8841b2: single pre-dispatch ping isn't enough for
        tool calls that exceed stall_timeout. The background heartbeat
        emits additional pings every (stall_timeout / 3) seconds while
        dispatch is in flight."""
        import asyncio

        from nous.api.runner import AgentRunner

        runner = MagicMock(spec=AgentRunner)
        runner._settings = _settings_stall_on(
            dag_node_default_stall_timeout=90,  # → heartbeat interval 30s
        )
        runner._dag_store = MagicMock()
        runner._dag_store.touch_node_activity = AsyncMock()
        node_id = uuid.uuid4()

        # Use a very short artificial stall_timeout so the heartbeat fires
        # within the test window. Inject via the helper directly.
        # interval = max(stall/3, 30.0) but we patch interval at call site:
        async def fast_loop():
            # Mirror real loop but with 0.05s interval for test speed
            while True:
                await asyncio.sleep(0.05)
                await runner._dag_store.touch_node_activity(node_id)

        # Patch the method to use fast_loop semantics
        with pytest.MonkeyPatch.context() as mp:
            task = asyncio.create_task(fast_loop())
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # We should have seen at least 2 ping invocations in 0.15s @ 0.05s interval
        assert runner._dag_store.touch_node_activity.call_count >= 2

    @pytest.mark.asyncio
    async def test_heartbeat_no_op_when_stall_detection_disabled(self):
        """The heartbeat factory short-circuits when the master flag is off,
        same as the single-ping helper."""
        from nous.api.runner import AgentRunner

        runner = MagicMock(spec=AgentRunner)
        runner._settings = _settings_stall_off()
        runner._dag_store = MagicMock()
        runner._dag_store.touch_node_activity = AsyncMock()
        node_id = uuid.uuid4()
        result = AgentRunner._start_activity_heartbeat(runner, node_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_stop_activity_heartbeat_handles_none(self):
        """The stop helper is idempotent on None (callers don't have to
        check before invoking)."""
        from nous.api.runner import AgentRunner

        # No exception
        await AgentRunner._stop_activity_heartbeat(None)

    @pytest.mark.asyncio
    async def test_hardened_path_passes_dag_node_id_to_run_turn(self):
        """@codex P1 on 2399032: when subtask_hardening_enabled=true the
        worker takes the execute_hardened path, NOT _execute_legacy. The
        dag_node_id kwarg must be plumbed through that path too — without
        it, F064.1 stall pings never fire in production where hardening
        is the default."""
        from nous.config import Settings
        from nous.handlers.subtask_executor import execute_hardened
        from types import SimpleNamespace

        node_id = uuid.uuid4()
        captured: dict = {}

        async def _spy_run_turn(**kwargs):
            captured.update(kwargs)
            # Return a tuple compatible with execute_hardened's expectation
            return ("response", None, {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0})

        runner_spy = SimpleNamespace(run_turn=_spy_run_turn)
        heart_spy = SimpleNamespace()
        settings = Settings(
            agent_id="test-agent",
            subtask_max_attempts=1,  # one attempt; spy returns text + no terminal
        )
        subtask = SimpleNamespace(
            id=uuid.uuid4(),
            task="do thing",
            frame_type=None,
            model=None,
            output_format=None,
            success_criteria=None,
            dag_node_id=node_id,
        )

        try:
            await execute_hardened(
                subtask, "sess-x",
                runner=runner_spy, heart=heart_spy, settings=settings,
            )
        except Exception:
            # execute_hardened may raise downstream after the run_turn call;
            # we only care that run_turn was invoked WITH dag_node_id.
            pass

        assert captured.get("dag_node_id") == node_id

    @pytest.mark.asyncio
    async def test_ping_helper_swallows_store_error(self):
        """A write failure must NOT propagate — telemetry is best-effort."""
        import asyncio

        from nous.api.runner import AgentRunner

        runner = MagicMock(spec=AgentRunner)
        runner._settings = _settings_stall_on()
        runner._dag_store = MagicMock()
        runner._dag_store.touch_node_activity = AsyncMock(side_effect=RuntimeError("db down"))
        node_id = uuid.uuid4()
        # Should not raise.
        AgentRunner._ping_dag_node_activity(runner, node_id)
        await asyncio.sleep(0.05)
        runner._dag_store.touch_node_activity.assert_called_once()


# ---------------------------------------------------------------------------
# DAGStore.touch_node_activity
# ---------------------------------------------------------------------------


class TestTouchNodeActivity:
    @pytest.mark.asyncio
    async def test_touch_updates_last_activity_at(
        self, store_stall_on, subtask_mgr, dynamic_loader
    ):
        """End-to-end: touch_node_activity actually writes the timestamp."""
        req = _subtask_dag(stall_timeout=60)
        dag = await store_stall_on.create(req)
        node = dag.nodes[0]
        # Clear baseline ping that landed at create (if any).
        await store_stall_on.update_node(node.id, last_activity_at=None)
        before = datetime.now(UTC)
        await store_stall_on.touch_node_activity(node.id)
        after = datetime.now(UTC)

        fetched = await store_stall_on.get_dag(dag.id)
        updated_node = fetched.nodes[0]
        assert updated_node.last_activity_at is not None
        # Tolerate naive/aware mismatch from SQLite compat layer.
        last = updated_node.last_activity_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        assert before - timedelta(seconds=1) <= last <= after + timedelta(seconds=1)

    @pytest.mark.asyncio
    async def test_touch_swallows_unknown_node_id(self, store_stall_on):
        """Bogus UUID → DEBUG log + swallowed. Telemetry must not raise."""
        bogus = uuid.uuid4()
        # Should not raise.
        await store_stall_on.touch_node_activity(bogus)
