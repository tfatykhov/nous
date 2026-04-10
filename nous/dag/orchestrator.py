"""F038: DAG Orchestrator — state machine that advances DAGs on each tick."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from nous.dag.store import DAGStore
from nous.storage.models import DAGNode, ExecutionDAG

if TYPE_CHECKING:
    from nous.events import EventBus
    from nous.heart.subtasks import SubtaskManager
    from nous.heartbeat.dynamic import DynamicCheckLoader

logger = logging.getLogger(__name__)

# Terminal statuses — nodes in these states won't be touched
_TERMINAL = frozenset({"completed", "failed", "blocked", "cancelled"})

# Budget warning threshold (80%)
_BUDGET_WARNING_RATIO = 0.80


class DAGOrchestrator:
    """Advances DAGs through their lifecycle on each heartbeat tick."""

    def __init__(
        self,
        store: DAGStore,
        subtask_mgr: SubtaskManager | None = None,
        dynamic_loader: DynamicCheckLoader | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._store = store
        self._subtask_mgr = subtask_mgr
        self._dynamic_loader = dynamic_loader
        self._bus = bus
        self._lock = asyncio.Lock()

    async def tick(self) -> int:
        """Advance all active DAGs. Returns number of DAGs processed.

        Protected by a lock to prevent concurrent tick races.
        """
        async with self._lock:
            dags = await self._store.get_active_dags()
            for dag in dags:
                try:
                    await self._advance_dag(dag)
                except Exception:
                    logger.exception("Error advancing DAG %s", dag.id)
            return len(dags)

    async def start_dag(self, dag_id: UUID) -> None:
        """Transition a pending DAG to running and launch wave-0 nodes."""
        dag = await self._store.get_dag(dag_id)
        if dag is None:
            raise ValueError(f"DAG {dag_id} not found")
        if dag.status != "pending":
            raise ValueError(f"DAG {dag_id} is {dag.status}, expected pending")

        await self._store.update_dag_status(dag_id, "running")

        # Re-fetch to get updated status
        dag = await self._store.get_dag(dag_id)
        if dag is None:
            return

        # Launch wave-0 ready nodes
        for node in dag.nodes:
            if node.status == "ready":
                await self._launch_node(node, dag)

    async def cancel_dag(self, dag_id: UUID, reason: str = "cancelled") -> None:
        """Cancel a DAG and all non-terminal nodes."""
        dag = await self._store.get_dag(dag_id)
        if dag is None:
            raise ValueError(f"DAG {dag_id} not found")

        for node in dag.nodes:
            if node.status not in _TERMINAL:
                await self._cancel_node(node)
                await self._store.update_node(
                    node.id, status="cancelled", error=reason
                )

        await self._store.update_dag_status(
            dag_id, "cancelled", result_summary=reason
        )

    async def retry_node(self, dag_id: UUID, node_name: str) -> None:
        """Reset a failed node to ready for re-execution."""
        dag = await self._store.get_dag(dag_id)
        if dag is None:
            raise ValueError(f"DAG {dag_id} not found")

        node = next((n for n in dag.nodes if n.name == node_name), None)
        if node is None:
            raise ValueError(f"Node '{node_name}' not found in DAG {dag_id}")
        if node.status != "failed":
            raise ValueError(f"Node '{node_name}' is {node.status}, expected failed")

        await self._store.update_node(
            node.id,
            status="ready",
            error=None,
            result=None,
            subtask_id=None,
            check_name=None,
            started_at=None,
            completed_at=None,
        )

    # ------------------------------------------------------------------
    # Internal: DAG advancement
    # ------------------------------------------------------------------

    async def _advance_dag(self, dag: ExecutionDAG) -> None:
        """Core state machine: sync → budget → failures → launch → complete."""
        # 1. Sync node statuses from underlying primitives
        await self._sync_node_statuses(dag)

        # 2. Check token budget
        if dag.token_budget:
            ratio = dag.tokens_consumed / dag.token_budget
            if ratio >= 1.0:
                logger.warning("DAG %s exceeded token budget", dag.id)
                await self._handle_budget_exceeded(dag)
                return
            elif ratio >= _BUDGET_WARNING_RATIO:
                logger.info(
                    "DAG %s at %.0f%% of token budget", dag.id, ratio * 100
                )

        # 3. Propagate failures
        await self._propagate_failures(dag)

        # 4. Find and launch ready nodes
        ready_nodes = self._find_ready_nodes(dag)
        for node in ready_nodes:
            await self._store.update_node(node.id, status="ready")
            node.status = "ready"  # Update in-memory too
            await self._launch_node(node, dag)

        # 5. Check if DAG is complete
        await self._check_dag_completion(dag)

    async def _sync_node_statuses(self, dag: ExecutionDAG) -> None:
        """Sync running node statuses from underlying subtasks/checks."""
        for node in dag.nodes:
            if node.status != "running":
                continue

            if node.node_type == "subtask" and node.subtask_id:
                await self._sync_subtask_node(node)
            elif node.node_type == "check" and node.check_name:
                await self._sync_check_node(node)

    async def _sync_subtask_node(self, node: DAGNode) -> None:
        """Sync a subtask node's status from the subtask manager."""
        if not self._subtask_mgr:
            return

        subtask = await self._subtask_mgr.get(node.subtask_id)
        if subtask is None:
            # Subtask was deleted — treat as failure
            await self._store.update_node(
                node.id,
                status="failed",
                error="Underlying subtask was deleted",
                completed_at=datetime.now(UTC),
            )
            node.status = "failed"
            return

        if subtask.status == "completed":
            await self._store.update_node(
                node.id,
                status="completed",
                result=subtask.result,
                completed_at=datetime.now(UTC),
            )
            node.status = "completed"
            node.result = subtask.result
        elif subtask.status == "failed":
            await self._store.update_node(
                node.id,
                status="failed",
                error=subtask.error or "Subtask failed",
                completed_at=datetime.now(UTC),
            )
            node.status = "failed"

    async def _sync_check_node(self, node: DAGNode) -> None:
        """Sync a check node's status from the check registry."""
        if not self._dynamic_loader:
            return

        check = self._dynamic_loader._registry.get_check(node.check_name)
        if check is None:
            # Check was unregistered — treat as failure
            await self._store.update_node(
                node.id,
                status="failed",
                error="Underlying check was unregistered",
                completed_at=datetime.now(UTC),
            )
            node.status = "failed"
            return

        # Use check.active to detect disabled checks (review fix)
        if not check.active:
            await self._store.update_node(
                node.id,
                status="completed",
                result="Check completed (disabled itself)",
                completed_at=datetime.now(UTC),
            )
            node.status = "completed"

    async def _propagate_failures(self, dag: ExecutionDAG) -> None:
        """Transitively block nodes whose predecessors have failed."""
        # Build dependency map: node_id -> set of predecessor node_ids
        dep_map: dict[str, set[str]] = {str(n.id): set() for n in dag.nodes}
        node_by_id: dict[str, DAGNode] = {str(n.id): n for n in dag.nodes}

        for edge in dag.edges:
            if edge.edge_type in ("dependency", "cancel_cascade"):
                dep_map[str(edge.to_node_id)].add(str(edge.from_node_id))

        # Find all failed node IDs
        failed_ids: set[str] = set()
        for node in dag.nodes:
            if node.status == "failed":
                failed_ids.add(str(node.id))

        if not failed_ids:
            return

        # Transitively find all nodes that depend on failed nodes
        to_block: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node_id, predecessors in dep_map.items():
                node = node_by_id[node_id]
                if node.status in _TERMINAL or node_id in to_block:
                    continue
                if predecessors & (failed_ids | to_block):
                    to_block.add(node_id)
                    changed = True

        # Block the affected nodes
        for node_id in to_block:
            node = node_by_id[node_id]
            await self._store.update_node(
                node.id, status="blocked", error="Predecessor failed"
            )
            node.status = "blocked"

    def _find_ready_nodes(self, dag: ExecutionDAG) -> list[DAGNode]:
        """Find pending nodes whose all dependency/context_flow predecessors are completed."""
        # Build set of predecessor node_ids per node (dependency + context_flow)
        dep_map: dict[str, set[str]] = {str(n.id): set() for n in dag.nodes}
        for edge in dag.edges:
            if edge.edge_type in ("dependency", "context_flow"):
                dep_map[str(edge.to_node_id)].add(str(edge.from_node_id))

        # Completed node IDs
        completed_ids = {str(n.id) for n in dag.nodes if n.status == "completed"}

        ready: list[DAGNode] = []
        for node in dag.nodes:
            if node.status != "pending":
                continue
            predecessors = dep_map[str(node.id)]
            if predecessors <= completed_ids:  # All predecessors completed
                ready.append(node)

        return ready

    async def _launch_node(self, node: DAGNode, dag: ExecutionDAG) -> None:
        """Create the underlying primitive and set node to running."""
        node_type = node.node_type

        if node_type == "subtask":
            await self._launch_subtask_node(node, dag)
        elif node_type == "check":
            await self._launch_check_node(node, dag)
        elif node_type == "gate":
            # Phase 1: auto-pass gates (Phase 2 will add Critic evaluation)
            await self._store.update_node(
                node.id,
                status="completed",
                result="Gate auto-passed (Phase 1)",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            node.status = "completed"
        elif node_type == "callback":
            # Mark completed with instructions as result
            await self._store.update_node(
                node.id,
                status="completed",
                result=node.instructions or "Callback completed",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            node.status = "completed"

    async def _launch_subtask_node(self, node: DAGNode, dag: ExecutionDAG) -> None:
        """Launch a subtask for this node."""
        if not self._subtask_mgr:
            await self._store.update_node(
                node.id,
                status="failed",
                error="No subtask manager available",
            )
            node.status = "failed"
            return

        # Build augmented instructions with predecessor context
        augmented = await self._build_predecessor_context(node, dag)

        try:
            subtask = await self._subtask_mgr.create(
                task=augmented,
                frame_type=node.frame_type,
                model=node.model,
                timeout=node.timeout_seconds,
                metadata={"dag_id": str(dag.id), "node_name": node.name},
            )
            await self._store.update_node(
                node.id,
                status="running",
                subtask_id=subtask.id,
                started_at=datetime.now(UTC),
            )
            node.status = "running"
            logger.info(
                "Launched subtask %s for node %s in DAG %s",
                subtask.id, node.name, dag.id,
            )
        except Exception as e:
            await self._store.update_node(
                node.id, status="failed", error=str(e)
            )
            node.status = "failed"
            logger.error(
                "Failed to launch subtask for node %s: %s", node.name, e
            )

    async def _launch_check_node(self, node: DAGNode, dag: ExecutionDAG) -> None:
        """Launch a dynamic check for this node."""
        if not self._dynamic_loader:
            await self._store.update_node(
                node.id,
                status="failed",
                error="No dynamic check loader available",
            )
            node.status = "failed"
            return

        augmented = await self._build_predecessor_context(node, dag)
        check_name = f"dag-{dag.id.hex[:8]}-{node.name}"

        try:
            await self._dynamic_loader.create_check(
                name=check_name,
                description=node.description or f"DAG check: {node.name}",
                prompt=augmented,
                tools=node.tools,
                interval_seconds=300,
                timeout_seconds=node.timeout_seconds,
            )
            await self._store.update_node(
                node.id,
                status="running",
                check_name=check_name,
                started_at=datetime.now(UTC),
            )
            node.status = "running"
            logger.info(
                "Launched check '%s' for node %s in DAG %s",
                check_name, node.name, dag.id,
            )
        except Exception as e:
            await self._store.update_node(
                node.id, status="failed", error=str(e)
            )
            node.status = "failed"
            logger.error(
                "Failed to launch check for node %s: %s", node.name, e
            )

    async def _build_predecessor_context(
        self, node: DAGNode, dag: ExecutionDAG
    ) -> str:
        """Collect results from context_flow predecessors and prepend to instructions."""
        # Find context_flow predecessors
        context_preds: list[str] = []
        for edge in dag.edges:
            if edge.edge_type == "context_flow" and str(edge.to_node_id) == str(node.id):
                context_preds.append(str(edge.from_node_id))

        if not context_preds:
            return node.instructions or ""

        # Build context from predecessor results
        parts: list[str] = []
        node_by_id = {str(n.id): n for n in dag.nodes}
        for pred_id in context_preds:
            pred = node_by_id.get(pred_id)
            if pred and pred.result:
                parts.append(f"[Result from '{pred.name}']: {pred.result}")

        context = "\n\n".join(parts)
        instructions = node.instructions or ""

        if context:
            return f"## Context from prior steps\n\n{context}\n\n## Instructions\n\n{instructions}"
        return instructions

    async def _cancel_node(self, node: DAGNode) -> None:
        """Cancel the underlying primitive for a node."""
        if node.node_type == "subtask" and node.subtask_id and self._subtask_mgr:
            try:
                from nous.storage.models import Subtask
                subtask = await self._subtask_mgr.get(node.subtask_id)
                if subtask and subtask.status == "pending":
                    await self._subtask_mgr.cancel(node.subtask_id)
            except Exception:
                logger.debug("Could not cancel subtask %s", node.subtask_id)

        elif node.node_type == "check" and node.check_name and self._dynamic_loader:
            try:
                await self._dynamic_loader.manage_check(
                    action="disable", name=node.check_name
                )
            except Exception:
                logger.debug("Could not disable check %s", node.check_name)

    async def _check_dag_completion(self, dag: ExecutionDAG) -> None:
        """Check if all nodes are terminal and set final DAG status."""
        statuses = {n.status for n in dag.nodes}

        # If any nodes are still non-terminal, DAG is not done
        non_terminal = statuses - _TERMINAL
        if non_terminal:
            return

        # All nodes are terminal — determine final status
        if all(n.status == "completed" for n in dag.nodes):
            await self._store.update_dag_status(
                dag.id, "completed", result_summary="All nodes completed successfully"
            )
        elif any(n.status == "failed" for n in dag.nodes):
            failed_names = [n.name for n in dag.nodes if n.status == "failed"]
            await self._store.update_dag_status(
                dag.id,
                "failed",
                result_summary=f"Failed nodes: {', '.join(failed_names)}",
            )
        elif any(n.status == "cancelled" for n in dag.nodes):
            await self._store.update_dag_status(
                dag.id, "cancelled", result_summary="DAG was cancelled"
            )
        else:
            # All blocked — still mark as failed
            await self._store.update_dag_status(
                dag.id, "failed", result_summary="All nodes blocked"
            )

    async def _handle_budget_exceeded(self, dag: ExecutionDAG) -> None:
        """Cancel pending/ready nodes when budget is exceeded."""
        cancelled_any = False
        for node in dag.nodes:
            if node.status in ("pending", "ready"):
                await self._store.update_node(
                    node.id, status="cancelled", error="Token budget exceeded"
                )
                node.status = "cancelled"
                cancelled_any = True

        # If there are still running nodes, let them finish
        has_running = any(n.status == "running" for n in dag.nodes)
        if not has_running:
            # Determine final status
            has_completed = any(n.status == "completed" for n in dag.nodes)
            if has_completed:
                await self._store.update_dag_status(
                    dag.id, "partial",
                    result_summary="Token budget exceeded, partial completion",
                )
            else:
                await self._store.update_dag_status(
                    dag.id, "failed",
                    result_summary="Token budget exceeded before any nodes completed",
                )
