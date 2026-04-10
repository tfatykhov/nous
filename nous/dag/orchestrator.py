"""F038: DAG Orchestrator — state machine that advances DAGs on each tick."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
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

# Completion check polling
_CHECK_CMD_TIMEOUT = 10.0  # Hard timeout per check command invocation
DAG_STATUS_BASE_DIR = Path(tempfile.gettempdir()) / "nous-workspace" / "dag-status"

CheckStatus = Literal["success", "failed", "pending"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a completion_check command invocation."""

    status: CheckStatus
    detail: str = ""


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
        async with self._lock:
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

        # Don't cancel already-terminal DAGs
        if dag.status in ("completed", "failed", "cancelled"):
            return

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
            status="pending",  # was "ready" — _find_ready_nodes only checks "pending"
            error=None,
            result=None,
            subtask_id=None,
            check_name=None,
            started_at=None,
            completed_at=None,
            check_attempts=0,
            last_check_at=None,
            awaiting_check_at=None,
        )

        # Selectively unblock only nodes downstream of the retried node
        # that have no other failed predecessors
        dep_map: dict[str, set[str]] = {str(n.id): set() for n in dag.nodes}
        for edge in dag.edges:
            if edge.edge_type in ("dependency", "cancel_cascade"):
                dep_map[str(edge.to_node_id)].add(str(edge.from_node_id))

        # Forward reachability from retried node
        adj: dict[str, list[str]] = {str(n.id): [] for n in dag.nodes}
        for edge in dag.edges:
            if edge.edge_type in ("dependency", "cancel_cascade"):
                adj[str(edge.from_node_id)].append(str(edge.to_node_id))

        reachable: set[str] = set()
        stack = [str(node.id)]
        while stack:
            nid = stack.pop()
            for child in adj.get(nid, []):
                if child not in reachable:
                    reachable.add(child)
                    stack.append(child)

        # IDs of nodes that are still failed (excluding the one being retried)
        still_failed = {
            str(n.id)
            for n in dag.nodes
            if n.status == "failed" and n.id != node.id
        }

        node_by_id = {str(n.id): n for n in dag.nodes}
        for nid in reachable:
            n = node_by_id[nid]
            if n.status != "blocked":
                continue
            # Only unblock if no other failed predecessor exists
            other_failed_preds = dep_map[nid] & still_failed
            if not other_failed_preds:
                await self._store.update_node(
                    n.id, status="pending", error=None
                )

        # Reactivate DAG if it was marked failed
        if dag.status in ("failed", "partial"):
            await self._store.update_dag_status(dag_id, "running")

    # ------------------------------------------------------------------
    # Internal: DAG advancement
    # ------------------------------------------------------------------

    async def _advance_dag(self, dag: ExecutionDAG) -> None:
        """Core state machine: sync → budget → failures → launch → complete."""
        # 1. Sync node statuses from underlying primitives
        await self._sync_node_statuses(dag)

        # 1.5 Poll awaiting_check nodes
        await self._poll_awaiting_checks(dag)

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
            if node.completion_check and node.completion_check.strip():
                # Subtask done but external process may still be running
                now = datetime.now(UTC)
                await self._store.update_node(
                    node.id,
                    status="awaiting_check",
                    result=subtask.result,
                    awaiting_check_at=now,
                )
                node.status = "awaiting_check"
                node.result = subtask.result
            else:
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

        registry = getattr(self._dynamic_loader, '_registry', None)
        check = registry.get_check(node.check_name) if registry else None
        if check is None:
            # Check was unregistered — for DAG-managed checks this means
            # self-disable completed (DynamicCheckLoader unregisters on disable).
            # Treat as successful completion, not failure.
            await self._store.update_node(
                node.id,
                status="completed",
                result="Check completed (self-disabled)",
                completed_at=datetime.now(UTC),
            )
            node.status = "completed"
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

    async def _poll_awaiting_checks(self, dag: ExecutionDAG) -> None:
        """Poll completion_check commands for nodes in awaiting_check status."""
        for node in dag.nodes:
            if node.status != "awaiting_check":
                continue

            cmd = node.completion_check
            if not cmd or not cmd.strip():
                # No valid check command — mark completed
                await self._store.update_node(
                    node.id, status="completed", completed_at=datetime.now(UTC),
                )
                node.status = "completed"
                continue

            # Check interval throttle
            if node.completion_check_interval and node.last_check_at:
                last = node.last_check_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                elapsed = (datetime.now(UTC) - last).total_seconds()
                if elapsed < node.completion_check_interval:
                    continue

            # Check timeout (based on awaiting_check_at, not started_at)
            ref_time = node.awaiting_check_at or node.started_at
            if ref_time:
                if ref_time.tzinfo is None:
                    ref_time = ref_time.replace(tzinfo=UTC)
                elapsed_total = (datetime.now(UTC) - ref_time).total_seconds()
                if elapsed_total > node.timeout_seconds:
                    await self._store.update_node(
                        node.id,
                        status="failed",
                        error=f"Completion check timed out after {node.timeout_seconds}s ({node.check_attempts} attempts)",
                        completed_at=datetime.now(UTC),
                    )
                    node.status = "failed"
                    continue

            # Check max attempts
            if node.max_check_attempts and node.check_attempts >= node.max_check_attempts:
                await self._store.update_node(
                    node.id,
                    status="failed",
                    error=f"Completion check exceeded max attempts ({node.max_check_attempts})",
                    completed_at=datetime.now(UTC),
                )
                node.status = "failed"
                continue

            # Run the completion check
            try:
                check = await self._run_completion_check(node)
                attempts = (node.check_attempts or 0) + 1
                now = datetime.now(UTC)

                if check.status == "success":
                    result = await self._read_node_result(node, dag)
                    await self._store.update_node(
                        node.id,
                        status="completed",
                        result=result,
                        check_attempts=attempts,
                        last_check_at=now,
                        completed_at=now,
                    )
                    node.status = "completed"
                    node.result = result
                    logger.info(
                        "Completion check passed for node %s (attempt %d)",
                        node.name, attempts,
                    )
                elif check.status == "failed":
                    error_msg = f"Completion check failed: {check.detail}" if check.detail else "Completion check failed (exit code 1)"
                    await self._store.update_node(
                        node.id,
                        status="failed",
                        error=error_msg,
                        check_attempts=attempts,
                        last_check_at=now,
                        completed_at=now,
                    )
                    node.status = "failed"
                    logger.warning(
                        "Completion check definitively failed for node %s (attempt %d): %s",
                        node.name, attempts, check.detail,
                    )
                else:
                    await self._store.update_node(
                        node.id, check_attempts=attempts, last_check_at=now,
                    )
                    node.check_attempts = attempts
                    node.last_check_at = now
                    logger.debug(
                        "Completion check pending for node %s (attempt %d)",
                        node.name, attempts,
                    )
            except Exception:
                logger.exception(
                    "Error polling completion check for node %s", node.name
                )

    async def _run_completion_check(self, node: DAGNode) -> CheckResult:
        """Run a completion_check shell command.

        Exit code semantics:
            0 → success (done)
            1 → failed (definitively)
            2 → pending (still running, keep polling)
        Any other exit code or error is treated as pending.
        """
        cmd = node.completion_check
        if not cmd:
            return CheckResult("success")

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_CHECK_CMD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning(
                    "Completion check command timed out (%.0fs) for node %s",
                    _CHECK_CMD_TIMEOUT, node.name,
                )
                return CheckResult("pending", "command timed out")

            if proc.returncode == 0:
                return CheckResult("success")
            elif proc.returncode == 1:
                detail = (stderr or b"").decode(errors="replace").strip()
                return CheckResult("failed", detail or "exit code 1")
            else:
                # Exit code 2 or anything else → still pending
                return CheckResult("pending")
        except Exception as e:
            logger.error("Completion check error for node %s: %s", node.name, e)
            return CheckResult("pending", str(e))

    async def _read_node_result(self, node: DAGNode, dag: ExecutionDAG) -> str | None:
        """Read result from status file convention, fall back to node's existing result."""
        status_dir = DAG_STATUS_BASE_DIR / dag.id.hex[:8] / node.name
        result_file = status_dir / "result"
        try:
            if result_file.exists():
                content = result_file.read_text().strip()
                if content:
                    return content
        except OSError:
            pass
        return node.result

    async def _propagate_failures(self, dag: ExecutionDAG) -> None:
        """Transitively block/cancel nodes whose predecessors have failed."""
        # Build separate maps for dependency and cancel_cascade edges
        dep_map: dict[str, set[str]] = {str(n.id): set() for n in dag.nodes}
        cancel_map: dict[str, set[str]] = {str(n.id): set() for n in dag.nodes}
        node_by_id: dict[str, DAGNode] = {str(n.id): n for n in dag.nodes}

        for edge in dag.edges:
            if edge.edge_type == "dependency":
                dep_map[str(edge.to_node_id)].add(str(edge.from_node_id))
            elif edge.edge_type == "cancel_cascade":
                cancel_map[str(edge.to_node_id)].add(str(edge.from_node_id))

        # Find all failed node IDs
        failed_ids: set[str] = set()
        for node in dag.nodes:
            if node.status == "failed":
                failed_ids.add(str(node.id))

        if not failed_ids:
            return

        # Transitively find nodes to block (dependency edges)
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

        # Find nodes to cancel (cancel_cascade edges — direct only)
        to_cancel: set[str] = set()
        for node_id, predecessors in cancel_map.items():
            node = node_by_id[node_id]
            if node.status in _TERMINAL or node_id in to_block:
                continue
            if predecessors & failed_ids:
                to_cancel.add(node_id)

        # Apply blocked status
        for node_id in to_block:
            node = node_by_id[node_id]
            await self._store.update_node(
                node.id, status="blocked", error="Predecessor failed"
            )
            node.status = "blocked"

        # Apply cancelled status
        for node_id in to_cancel:
            node = node_by_id[node_id]
            await self._cancel_node(node)
            await self._store.update_node(
                node.id, status="cancelled", error="Cancelled by predecessor failure"
            )
            node.status = "cancelled"

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
                subtask = await self._subtask_mgr.get(node.subtask_id)
                if subtask and subtask.status in ("pending", "running"):
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
            if node.status in ("pending", "ready", "awaiting_check"):
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
