"""F066.1 Phase 1 tests: fix-node state machine + rule-based dispatcher.

Covers:
- Pydantic validator: fix-node required fields, invalid actions, no-fix-of-fix,
  on_failure edge constraints, at-most-one-fix-child-per-parent.
- fix_executor.choose_action rule-based dispatch.
- Orchestrator end-to-end: parent fails → fix fires → action applied;
  fix_attempts_used cap; skipped state unblocks successors.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag.fix_executor import FixActionResult, choose_action
from nous.dag.orchestrator import DAGOrchestrator, _RESOLVED, _TERMINAL
from nous.dag.schemas import (
    DAGCreateRequest,
    DAGEdgeSpec,
    DAGNodeSpec,
    DAGNodeStatus,
    DAGNodeType,
)
from nous.dag.store import DAGStore


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(db):
    """DAGStore with a unique agent_id per test."""
    agent_id = f"f066-1-{uuid.uuid4().hex[:8]}"
    return DAGStore(db, agent_id, Settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    return mgr


@pytest.fixture
def dynamic_loader():
    loader = AsyncMock()
    loader.create_check = AsyncMock(return_value={"name": "t"})
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


@pytest.fixture
def orchestrator(store, subtask_mgr, dynamic_loader):
    return DAGOrchestrator(
        store=store,
        subtask_mgr=subtask_mgr,
        dynamic_loader=dynamic_loader,
        settings=Settings(),
    )


def _parent_with_fix_request(fix_actions: list[str] | None = None) -> DAGCreateRequest:
    """A DAG with one subtask parent + one fix child via on_failure edge."""
    if fix_actions is None:
        fix_actions = ["retry_as_is", "skip_and_continue"]
    return DAGCreateRequest(
        name="parent-with-fix-dag",
        nodes=[
            DAGNodeSpec(name="parent", type=DAGNodeType.subtask, instructions="do thing"),
            DAGNodeSpec(
                name="fix-parent",
                type=DAGNodeType.fix,
                parent_node="parent",
                fix_actions=fix_actions,
                instructions="diagnose and recover",
            ),
        ],
        edges=[
            DAGEdgeSpec(from_node="parent", to_node="fix-parent", edge_type="on_failure"),
        ],
    )


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------


class TestFixNodeValidator:
    def test_valid_fix_node_dag_accepted(self) -> None:
        req = _parent_with_fix_request()
        # No exception raised.
        assert any(n.type == DAGNodeType.fix for n in req.nodes)

    def test_fix_node_without_parent_node_rejected(self) -> None:
        with pytest.raises(ValueError, match="parent_node"):
            DAGCreateRequest(
                name="bad",
                nodes=[
                    DAGNodeSpec(name="p", type=DAGNodeType.subtask),
                    DAGNodeSpec(
                        name="fix",
                        type=DAGNodeType.fix,
                        fix_actions=["retry_as_is"],
                    ),
                ],
                edges=[DAGEdgeSpec(from_node="p", to_node="fix", edge_type="on_failure")],
            )

    def test_fix_node_without_fix_actions_rejected(self) -> None:
        with pytest.raises(ValueError, match="fix_actions"):
            DAGCreateRequest(
                name="bad",
                nodes=[
                    DAGNodeSpec(name="p", type=DAGNodeType.subtask),
                    DAGNodeSpec(name="fix", type=DAGNodeType.fix, parent_node="p"),
                ],
                edges=[DAGEdgeSpec(from_node="p", to_node="fix", edge_type="on_failure")],
            )

    def test_fix_node_invalid_action_rejected(self) -> None:
        with pytest.raises(ValueError):
            DAGCreateRequest(
                name="bad",
                nodes=[
                    DAGNodeSpec(name="p", type=DAGNodeType.subtask),
                    DAGNodeSpec(
                        name="fix",
                        type=DAGNodeType.fix,
                        parent_node="p",
                        fix_actions=["nonexistent_action"],
                    ),
                ],
                edges=[DAGEdgeSpec(from_node="p", to_node="fix", edge_type="on_failure")],
            )

    def test_fix_node_unknown_parent_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown parent_node"):
            DAGCreateRequest(
                name="bad",
                nodes=[
                    DAGNodeSpec(name="p", type=DAGNodeType.subtask),
                    DAGNodeSpec(
                        name="fix",
                        type=DAGNodeType.fix,
                        parent_node="ghost",
                        fix_actions=["retry_as_is"],
                    ),
                ],
                # Use an edge that references the fake parent so cycle/edge
                # validation passes — but the parent_node string is wrong.
                edges=[],
            )

    def test_fix_of_fix_rejected(self) -> None:
        with pytest.raises(ValueError, match="fix-of-fix"):
            DAGCreateRequest(
                name="bad",
                nodes=[
                    DAGNodeSpec(name="p", type=DAGNodeType.subtask),
                    DAGNodeSpec(
                        name="fix1",
                        type=DAGNodeType.fix,
                        parent_node="p",
                        fix_actions=["retry_as_is"],
                    ),
                    DAGNodeSpec(
                        name="fix2",
                        type=DAGNodeType.fix,
                        parent_node="fix1",  # FORBIDDEN
                        fix_actions=["retry_as_is"],
                    ),
                ],
                edges=[
                    DAGEdgeSpec(from_node="p", to_node="fix1", edge_type="on_failure"),
                    DAGEdgeSpec(from_node="fix1", to_node="fix2", edge_type="on_failure"),
                ],
            )

    def test_two_fix_children_per_parent_rejected(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            DAGCreateRequest(
                name="bad",
                nodes=[
                    DAGNodeSpec(name="p", type=DAGNodeType.subtask),
                    DAGNodeSpec(
                        name="fix1",
                        type=DAGNodeType.fix,
                        parent_node="p",
                        fix_actions=["retry_as_is"],
                    ),
                    DAGNodeSpec(
                        name="fix2",
                        type=DAGNodeType.fix,
                        parent_node="p",
                        fix_actions=["skip_and_continue"],
                    ),
                ],
                edges=[
                    DAGEdgeSpec(from_node="p", to_node="fix1", edge_type="on_failure"),
                    DAGEdgeSpec(from_node="p", to_node="fix2", edge_type="on_failure"),
                ],
            )

    def test_fix_node_without_on_failure_edge_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one on_failure"):
            DAGCreateRequest(
                name="bad",
                nodes=[
                    DAGNodeSpec(name="p", type=DAGNodeType.subtask),
                    DAGNodeSpec(
                        name="fix",
                        type=DAGNodeType.fix,
                        parent_node="p",
                        fix_actions=["retry_as_is"],
                    ),
                ],
                edges=[],  # missing on_failure
            )


# ---------------------------------------------------------------------------
# Enum membership
# ---------------------------------------------------------------------------


class TestEnumExtensions:
    def test_fix_in_dag_node_type(self) -> None:
        assert DAGNodeType.fix == "fix"

    def test_skipped_in_dag_node_status(self) -> None:
        assert DAGNodeStatus.skipped == "skipped"

    def test_skipped_in_terminal_set(self) -> None:
        assert "skipped" in _TERMINAL

    def test_skipped_in_resolved_set(self) -> None:
        """Successors of a skipped node must unblock — skipped is resolved."""
        assert "skipped" in _RESOLVED
        # Sanity: 'failed' is NOT resolved (otherwise cascade would never fire).
        assert "failed" not in _RESOLVED


# ---------------------------------------------------------------------------
# Rule-based dispatcher (fix_executor.choose_action)
# ---------------------------------------------------------------------------


class TestChooseAction:
    def test_incomplete_no_terminal_retries(self) -> None:
        out = choose_action(
            parent_error="validation: incomplete_no_terminal — submit_final_report missing",
            parent_status="failed",
            fix_actions=["retry_as_is", "skip_and_continue"],
        )
        assert out.action == "retry_as_is"

    def test_validation_failed_retries(self) -> None:
        out = choose_action(
            parent_error="schema_invalid: validation_failed on payload",
            parent_status="failed",
            fix_actions=["retry_as_is", "skip_and_continue"],
        )
        assert out.action == "retry_as_is"

    def test_timed_out_prefers_skip(self) -> None:
        out = choose_action(
            parent_error="Timeout after 600s — timed_out",
            parent_status="failed",
            fix_actions=["retry_as_is", "skip_and_continue"],
        )
        assert out.action == "skip_and_continue"

    def test_timed_out_falls_back_to_unrecoverable_if_no_skip(self) -> None:
        out = choose_action(
            parent_error="timed_out: deadline",
            parent_status="failed",
            fix_actions=["retry_as_is"],
        )
        assert out.action == "mark_unrecoverable"

    def test_unknown_error_prefers_skip(self) -> None:
        out = choose_action(
            parent_error="random unexpected exception",
            parent_status="failed",
            fix_actions=["skip_and_continue", "mark_unrecoverable"],
        )
        assert out.action == "skip_and_continue"

    def test_mark_unrecoverable_is_implicit_last_resort(self) -> None:
        """Even if fix_actions is empty, mark_unrecoverable is always returnable."""
        out = choose_action(
            parent_error="anything",
            parent_status="failed",
            fix_actions=[],
        )
        assert out.action == "mark_unrecoverable"

    def test_none_error_handled(self) -> None:
        out = choose_action(
            parent_error=None,
            parent_status="failed",
            fix_actions=["skip_and_continue"],
        )
        # Empty error → no specific match → prefers skip if available.
        assert out.action == "skip_and_continue"


# ---------------------------------------------------------------------------
# Orchestrator end-to-end
# ---------------------------------------------------------------------------


class TestOrchestratorFixApplication:
    """End-to-end: tick processes parent failure → fix fires → action applied."""

    @pytest.mark.asyncio
    async def test_fix_node_inserted_as_pending_not_ready(
        self, store, orchestrator
    ):
        """A wave-0 fix node MUST NOT be dispatched at start_dag time —
        it sits 'pending' until parent fails."""
        dag = await store.create(_parent_with_fix_request())
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        fix_node = next(n for n in fetched.nodes if n.node_type == "fix")
        assert fix_node.status == "pending"

    @pytest.mark.asyncio
    async def test_retry_action_resets_parent_to_pending(
        self, store, orchestrator, subtask_mgr
    ):
        dag = await store.create(_parent_with_fix_request(["retry_as_is"]))
        await orchestrator.start_dag(dag.id)

        # Simulate parent failure with a recoverable error.
        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(
            parent.id, status="failed", error="validation_failed: payload"
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        fix_node = next(n for n in fetched.nodes if n.node_type == "fix")

        # Parent re-enqueued for dispatch.
        assert parent.status in ("pending", "ready", "running")
        # Fix attempts consumed.
        assert fix_node.fix_attempts_used == 1
        # Error cleared on retry.
        assert parent.error is None or parent.error == ""

    @pytest.mark.asyncio
    async def test_skip_action_marks_parent_skipped(
        self, store, orchestrator
    ):
        dag = await store.create(
            _parent_with_fix_request(["skip_and_continue"])
        )
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(
            parent.id, status="failed", error="timed_out: deadline exceeded"
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        assert parent.status == "skipped"
        # result records the fix rationale
        assert parent.result and "skip" in parent.result.lower()

    @pytest.mark.asyncio
    async def test_mark_unrecoverable_keeps_failed(
        self, store, orchestrator
    ):
        # Only mark_unrecoverable is allowed (no retry, no skip).
        dag = await store.create(
            _parent_with_fix_request(["mark_unrecoverable"])
        )
        await orchestrator.start_dag(dag.id)

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(
            parent.id, status="failed", error="unknown panic"
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        assert parent.status == "failed"
        assert parent.result and "unrecoverable" in parent.result.lower()

    @pytest.mark.asyncio
    async def test_fix_attempts_cap_prevents_infinite_retry(
        self, store, orchestrator
    ):
        dag = await store.create(_parent_with_fix_request(["retry_as_is"]))
        await orchestrator.start_dag(dag.id)

        # Manually consume the fix budget by setting fix_attempts_used to max.
        fetched = await store.get_dag(dag.id)
        fix_node = next(n for n in fetched.nodes if n.node_type == "fix")
        await store.update_node(
            fix_node.id, fix_attempts_used=fix_node.max_fix_attempts,
        )

        parent = next(n for n in fetched.nodes if n.name == "parent")
        await store.update_node(
            parent.id, status="failed", error="validation_failed"
        )

        await orchestrator.tick()

        fetched = await store.get_dag(dag.id)
        parent = next(n for n in fetched.nodes if n.name == "parent")
        # Budget exhausted — fix does not re-fire; parent stays failed.
        assert parent.status == "failed"
