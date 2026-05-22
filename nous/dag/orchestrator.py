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

from nous.config import Settings
from nous.dag._workspace import assert_inside_root, compute_workspace_path
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

# Grace period before a 'ready' node in a running DAG is demoted to 'pending'
# so the tick loop can pick it up. Wave-0 nodes normally transition immediately
# via start_dag(); this constant covers the case where that path was bypassed.
_STALE_READY_GRACE_SECONDS = 300
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
        *,
        settings: Settings,
    ) -> None:
        self._store = store
        self._subtask_mgr = subtask_mgr
        self._dynamic_loader = dynamic_loader
        self._bus = bus
        self._settings = settings
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

            # F064.2: route wave-0 launches through _dispatch_ready_nodes so
            # per-frame-type concurrency caps apply on the very first tick.
            # When the flag is off, the helper falls back to today's behavior
            # (launch every ready node). Per-node try/except inside the helper
            # also gives us the silent-failure P1-6 guarantee on this path.
            wave_zero = [n for n in dag.nodes if n.status == "ready"]
            await self._dispatch_ready_nodes(dag, wave_zero)

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
            if n.status not in ("blocked", "cancelled"):
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
        """Core state machine: sync → stall → budget → failures → launch → complete."""
        # 1. Sync node statuses from underlying primitives
        await self._sync_node_statuses(dag)

        # 1.5 Poll awaiting_check nodes
        await self._poll_awaiting_checks(dag)

        # 1.7 F064.1: Stall detection. Runs after sync (so just-completed
        # nodes are no longer 'running') and before failure-propagation
        # (so a node marked stalled here cascades on the same tick).
        if self._settings.dag_stall_detection_enabled:
            await self._check_stalled_nodes(dag)

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

        # 2.5. Recover orphaned wave-0 nodes that are stuck in 'ready' status
        # because start_dag() was bypassed or the dispatch failed silently.
        await self._recover_stale_ready_nodes(dag)

        # 3. Propagate failures
        await self._propagate_failures(dag)

        # 4. Find and launch ready nodes (F064.2 dispatch with optional per-
        # frame caps; falls back to legacy behavior when flag is off).
        ready_nodes = self._find_ready_nodes(dag)
        await self._dispatch_ready_nodes(dag, ready_nodes)

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

        # F061: outcome-aware branch. Inverse check (any non-"completed"
        # outcome fails the DAG node) rather than a closed set of failure
        # names — keeps the branch forward-compatible if a future PR adds
        # outcome enum values like timed_out_bootstrap, censor_blocked, etc.
        # ``final is None`` means a pre-flag row → falls through to legacy
        # status-based branching below.
        final = getattr(subtask, "final_outcome", None)
        if final is not None and final != "completed":
            report = getattr(subtask, "report_jsonb", None) or {}
            blocked_reason = (
                report.get("blocked_reason")
                if final == "incomplete_blocked" and isinstance(report, dict)
                else None
            )
            error_msg = (
                f"subtask {final}: "
                f"{subtask.error or blocked_reason or 'no reason'}"
            )
            await self._store.update_node(
                node.id,
                status="failed",
                error=error_msg,
                result=subtask.result,
                completed_at=datetime.now(UTC),
            )
            node.status = "failed"
            node.error = error_msg
            node.result = subtask.result
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
            if node.completion_check and node.completion_check.strip():
                # Heartbeat check finished — now poll the shell completion_check
                now = datetime.now(UTC)
                await self._store.update_node(
                    node.id,
                    status="awaiting_check",
                    awaiting_check_at=now,
                )
                node.status = "awaiting_check"
            else:
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
            if node.completion_check and node.completion_check.strip():
                # Heartbeat check disabled itself — now poll the shell completion_check
                now = datetime.now(UTC)
                await self._store.update_node(
                    node.id,
                    status="awaiting_check",
                    awaiting_check_at=now,
                )
                node.status = "awaiting_check"
            else:
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
                effective_timeout = self._effective_timeout(node)
                if elapsed_total > effective_timeout:
                    await self._store.update_node(
                        node.id,
                        status="failed",
                        error=f"Completion check timed out after {effective_timeout}s ({node.check_attempts} attempts)",
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

        F064.3: subprocess runs with `cwd=workspace_path` so a relative
        path inside the check command resolves under the node's workspace
        rather than the orchestrator process's cwd. The workspace dir is
        ensured to exist (mkdir parents) before the subprocess launch so
        ``create_subprocess_shell`` doesn't FileNotFoundError on a fresh
        node. Containment is asserted unconditionally as a security boundary.
        """
        cmd = node.completion_check
        if not cmd:
            return CheckResult("success")

        # F064.3: derive the per-node workspace + assert containment. The
        # dag attribute isn't on the node directly; we read it from the
        # parent DAG row via the FK. The completion-check subprocess MUST
        # run with cwd inside the workspace root — if we can't derive a
        # safe workspace, FAIL the check rather than fall through to
        # cwd=None (which would run in the orchestrator process's working
        # directory and defeat the security boundary).
        # @codex P2 on 37fc50f: previously this branch logged+fell through
        # to cwd=None, which is the exact behavior the containment check
        # is meant to prevent.
        cwd_arg: str
        try:
            dag = await self._store.get_dag(node.dag_id)
            if dag is None:
                return CheckResult(
                    "failed",
                    f"workspace setup failed: DAG {node.dag_id} not found",
                )
            root = self._settings.dag_workspace_root
            workspace = compute_workspace_path(dag.id, node.name, root)
            assert_inside_root(workspace, root)
            workspace.mkdir(parents=True, exist_ok=True)
            cwd_arg = str(workspace)
        except (ValueError, OSError) as e:
            logger.warning(
                "F064.3: completion_check workspace setup failed for node %s — %s; "
                "failing the check (refusing to run with unsafe cwd)",
                node.name, e,
            )
            return CheckResult(
                "failed",
                f"workspace containment failure: {e}",
            )

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd_arg,
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

    def _effective_timeout(self, node: DAGNode) -> int:
        """Clamp node.timeout_seconds to settings.dag_node_max_timeout.

        Defensive re-clamp: store already clamps at insert, but historical rows
        or direct DB writes may carry values above the current ceiling.
        """
        return min(node.timeout_seconds, self._settings.dag_node_max_timeout)

    def _effective_stall_timeout(self, node: DAGNode) -> int | None:
        """F064.1: resolve per-node stall timeout, applying defaults/clamps.

        Returns None when stall detection should be skipped for this node.

        Cascade semantics (matches DAGNodeSpec description):
        - per-node `stall_timeout_seconds` is None  → fall back to the operator
          default `NOUS_DAG_NODE_DEFAULT_STALL_TIMEOUT`. Setting the default to
          0 globally disables for unset rows; this is how operators opt out of
          stall detection for pre-existing data without touching the DAGNodeSpec
          field. (Per @codex P1 on 9ee630a: docs+code were inconsistent — the
          spec field description previously said "None disables" which was
          wrong; corrected on DAGNodeSpec to read "None = use global default".)
        - per-node `stall_timeout_seconds` is 0     → explicitly disabled
          PER NODE, regardless of the global default. Mirrors Symphony §8.5
          semantics for `stall_timeout_ms <= 0`.
        - otherwise clamp to settings.dag_node_max_stall_timeout.
        """
        per_node = node.stall_timeout_seconds
        if per_node is None:
            default = self._settings.dag_node_default_stall_timeout
            return default if default > 0 else None
        if per_node == 0:
            return None
        return min(per_node, self._settings.dag_node_max_stall_timeout)

    async def _check_stalled_nodes(self, dag: ExecutionDAG) -> None:
        """F064.1: mark running nodes failed when no activity ping arrived in time.

        Policy (plan §4.3):
        - Only inspects status='running' nodes (sync already moved completed ones)
        - NULL last_activity_at → NOT flagged (wall-clock is the fallback). This
          covers (a) brand-new nodes between launch and first ping and (b) the
          legitimate case where all three ping sites failed silently.
        - Cascade is left to the existing _propagate_failures call later in the
          same tick — we just flip status to failed here.
        """
        now = datetime.now(UTC)
        for node in dag.nodes:
            if node.status != "running":
                continue
            stall = self._effective_stall_timeout(node)
            if stall is None:
                continue
            last = node.last_activity_at
            if last is None:
                # No ping yet — treat as not-stalled. Wall-clock timeout is
                # the fallback. See plan §4.3 NULL-fallback policy.
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            elapsed = (now - last).total_seconds()
            if elapsed > stall:
                error_msg = f"stalled: no activity for {elapsed:.0f}s (limit {stall}s)"
                logger.warning(
                    "F064.1: marking node %s (dag %s) failed — %s",
                    node.name, dag.id, error_msg,
                )
                # @codex P1 on 9ee630a: tear down the underlying primitive
                # BEFORE marking the node failed. Without this, the subtask
                # keeps running (consuming tokens, holding worker slots) and
                # is no longer tracked by _sync_node_statuses (which only
                # processes nodes still marked 'running'). _cancel_node is
                # exception-safe — its own try/except absorbs "couldn't
                # cancel" cases so stall handling never blocks on cleanup.
                await self._cancel_node(node)
                await self._store.update_node(
                    node.id,
                    status="failed",
                    error=error_msg,
                    completed_at=now,
                )
                node.status = "failed"
                node.error = error_msg
                node.completed_at = now

    async def _read_node_result(self, node: DAGNode, dag: ExecutionDAG) -> str | None:
        """Read result from status file convention, fall back to node's existing result.

        F064.3: routes through compute_workspace_path (lenient transformation
        of legacy unsafe node names → safe path equivalents) and asserts
        containment under the configured workspace root UNCONDITIONALLY,
        regardless of dag_workspace_safety_enabled. Path traversal is a
        security boundary, not a feature — even pre-flag rows must not
        escape the root via symlink or naive ``..``.
        """
        root = self._settings.dag_workspace_root
        try:
            status_dir = compute_workspace_path(dag.id, node.name, root)
            assert_inside_root(status_dir, root)
        except ValueError as e:
            logger.warning(
                "F064.3: refusing to read result for node %s in DAG %s — %s",
                node.name, dag.id, e,
            )
            return node.result
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

        # Find nodes to cancel (cancel_cascade edges — direct only)
        to_cancel: set[str] = set()
        for node_id, predecessors in cancel_map.items():
            node = node_by_id[node_id]
            if node.status in _TERMINAL:
                continue
            if predecessors & failed_ids:
                to_cancel.add(node_id)

        # Transitively find nodes to block (dependency edges)
        # Treat both failed and cancel_cascade-cancelled nodes as "poison"
        poison = failed_ids | to_cancel
        to_block: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node_id, predecessors in dep_map.items():
                node = node_by_id[node_id]
                if node.status in _TERMINAL or node_id in to_block or node_id in to_cancel:
                    continue
                if predecessors & (poison | to_block):
                    to_block.add(node_id)
                    changed = True

        # Apply cancelled status (cancel_cascade targets)
        for node_id in to_cancel:
            node = node_by_id[node_id]
            await self._cancel_node(node)
            await self._store.update_node(
                node.id, status="cancelled", error="Cancelled by predecessor failure"
            )
            node.status = "cancelled"

        # Apply blocked status (dependency descendants)
        for node_id in to_block:
            node = node_by_id[node_id]
            await self._store.update_node(
                node.id, status="blocked", error="Predecessor failed"
            )
            node.status = "blocked"

    def _effective_frame_caps(self, dag: ExecutionDAG) -> dict[str, int]:
        """F064.2: resolve the effective per-frame-type caps for this DAG.

        Operator-level env override wins over per-DAG when set (matches
        Symphony §8.3 "per-state limit takes precedence over global limit"
        semantics, except here it's a hierarchical merge rather than
        replacement so unset frames keep per-DAG caps).

        Returns an empty dict when neither source has caps — caller skips
        gating entirely and dispatches as before (today's behavior).
        """
        per_dag = dag.max_concurrent_by_frame_type or {}
        env_override = self._settings.dag_global_max_concurrent_by_frame or {}
        if not per_dag and not env_override:
            return {}
        # Env override takes precedence per-frame; per-DAG fills the rest.
        merged = dict(per_dag)
        merged.update(env_override)
        return merged

    async def _dispatch_ready_nodes(
        self, dag: ExecutionDAG, ready_nodes: list[DAGNode]
    ) -> None:
        """F064.2: gated dispatch of ready nodes with per-frame-type concurrency caps.

        Backward-compatible: when dag_frame_concurrency_enabled=False or no
        caps are configured, dispatches every ready node in the same tick —
        identical to pre-F064.2 behavior.

        When caps are active, consults a single grouped SELECT on
        heart.subtasks at the start of the tick, then accumulates in-memory
        for each successful launch so we don't over-dispatch within one tick
        (the DB count won't refresh between awaits inside the same tick).

        Per-node try/except wraps _launch_node in both legacy and capped paths
        (review silent-failure P1-6 / conventions P2-6) — a single failed
        launch logs+continues so the remaining wave still dispatches.
        """
        # Backward-compat: flag off → legacy behavior, just with the new
        # per-node error guard. No DB count, no caps.
        if not self._settings.dag_frame_concurrency_enabled:
            for node in ready_nodes:
                await self._store.update_node(node.id, status="ready")
                node.status = "ready"
                try:
                    await self._launch_node(node, dag)
                except Exception:
                    logger.exception(
                        "Failed to launch node %s in DAG %s", node.name, dag.id
                    )
            return

        caps = self._effective_frame_caps(dag)
        if not caps:
            for node in ready_nodes:
                await self._store.update_node(node.id, status="ready")
                node.status = "ready"
                try:
                    await self._launch_node(node, dag)
                except Exception:
                    logger.exception(
                        "Failed to launch node %s in DAG %s", node.name, dag.id
                    )
            return

        # @codex P1 on aa3c739: scope to current DAG so concurrent DAGs don't
        # consume each other's slots. Per-DAG cap semantics now align with
        # per-DAG count semantics.
        running_by_frame = await self._store.count_running_subtasks_by_frame_type(
            dag_id=dag.id
        )
        for node in ready_nodes:
            # @codex P2 on 48589fd: count source (heart.subtasks) only sees
            # subtask nodes — check/gate/callback nodes don't create a
            # subtask row, so counting them against the cap would over-
            # restrict and not counting them at all would let them bypass
            # the cap. The conservative choice is to only ENFORCE the cap
            # for subtask nodes: check/gate/callback always launch (they
            # have no resource cost the cap is meant to bound).
            if node.node_type != "subtask":
                await self._store.update_node(node.id, status="ready")
                node.status = "ready"
                try:
                    await self._launch_node(node, dag)
                except Exception:
                    logger.exception(
                        "Failed to launch node %s in DAG %s", node.name, dag.id
                    )
                continue

            frame = node.frame_type if node.frame_type is not None else "_default"
            cap = caps.get(frame)
            if cap is not None and running_by_frame.get(frame, 0) >= cap:
                # @codex P1 on ab02178: wave-0 nodes arrive at this branch in
                # status='ready' (set by store.create). _find_ready_nodes only
                # picks up 'pending' nodes, so a deferred wave-0 node would be
                # stuck in 'ready' forever. Demote the row to 'pending' on
                # deferral — both wave-0 and wave-N use the same semantic
                # afterward: deferred = pending, re-picked next tick.
                if node.status == "ready":
                    await self._store.update_node(node.id, status="pending")
                    node.status = "pending"
                logger.debug(
                    "F064.2: deferring node %s (frame=%s) — cap %d reached",
                    node.name, frame, cap,
                )
                continue
            await self._store.update_node(node.id, status="ready")
            node.status = "ready"
            try:
                await self._launch_node(node, dag)
            except Exception:
                logger.exception(
                    "Failed to launch node %s in DAG %s", node.name, dag.id
                )
                # Slot wasn't consumed — don't bump the accumulator.
                continue
            # _launch_subtask_node swallows its own exceptions internally and
            # sets node.status="failed". Only bump the accumulator when the
            # launch actually produced a 'running' subtask. Other terminal
            # outcomes (callback/gate set status="completed", subtask launch
            # error sets "failed") don't consume a running-slot.
            if node.status == "running":
                running_by_frame[frame] = running_by_frame.get(frame, 0) + 1

    async def _recover_stale_ready_nodes(self, dag: ExecutionDAG) -> None:
        """Demote orphaned 'ready' nodes to 'pending' after the grace period.

        Wave-0 nodes are created with status='ready' and are supposed to be
        transitioned by start_dag(). If that path was bypassed (e.g. direct
        DB writes via psql, as in issue #430) the nodes sit invisible to
        _find_ready_nodes() forever. After _STALE_READY_GRACE_SECONDS the
        sweep demotes them to 'pending' so the normal tick loop dispatches
        them.

        Covers TWO bypass scenarios:

        1. ``status='pending'`` with ``started_at=NULL`` — psql INSERT path
           from issue #430. start_dag() never ran. Reference time is
           dag.created_at (always set by the INSERT). When this recovery
           fires we ALSO transition the DAG to 'running' via
           ``update_dag_status`` so downstream tick-loop steps
           (_check_dag_completion, _propagate_failures, budget enforcement)
           see a consistent 'running with dispatched wave-0' state — the
           same invariant start_dag() would have produced.

        2. ``status='running'`` but wave-0 still in 'ready' — start_dag()
           ran (started_at set) but the dispatch never landed (a bug we
           don't have a concrete repro for, but Codex flagged the parity
           gap). Reference time is dag.started_at.

        Grace window is 300s by default. The legitimate dag_create →
        start_dag window is sub-second (called inline in the same tool
        — orchestrator.start_dag is invoked at the end of dag_create's
        body), so 5 minutes dwarfs it without risk of false-positive
        recovery.
        """
        if dag.status not in ("pending", "running"):
            return

        # Reference time: prefer started_at (running case), fall back to
        # created_at (pending bypass case). created_at is NOT NULL — even
        # a raw INSERT populates it via server_default=now().
        ref = dag.started_at or dag.created_at
        if ref is None:
            # Defensive: should never happen given created_at NOT NULL.
            return
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        dag_age_seconds = (now - ref).total_seconds()
        if dag_age_seconds < _STALE_READY_GRACE_SECONDS:
            return

        # Identify orphan ready nodes first; only mutate state if at least
        # one is recoverable. Avoids promoting a 'pending' DAG to 'running'
        # when there's nothing to dispatch.
        recoverable = [
            n for n in dag.nodes
            if n.status == "ready" and n.started_at is None
        ]
        if not recoverable:
            return

        # For bypass scenario 1: promote the DAG itself to 'running' so
        # downstream _advance_dag steps operate on the canonical invariant
        # (running DAG with dispatched wave-0). This also populates
        # started_at via update_dag_status, fixing the underlying bypass.
        if dag.status == "pending":
            await self._store.update_dag_status(dag.id, "running")
            dag.status = "running"
            if dag.started_at is None:
                dag.started_at = now
            logger.warning(
                "Recovered orphaned pending DAG %s (age %.0fs, %d ready "
                "nodes) — promoted to 'running' so tick loop can dispatch",
                dag.id, dag_age_seconds, len(recoverable),
            )

        for node in recoverable:
            await self._store.update_node(node.id, status="pending")
            node.status = "pending"
            logger.warning(
                "Recovered stale ready node '%s' in DAG %s "
                "(DAG age %.0fs with no dispatch) — demoted to pending "
                "so tick loop can dispatch it",
                node.name, dag.id, dag_age_seconds,
            )

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
            # F061 PR-3 Codex round 5: pass dag_node_id so the dashboard's
            # dag_correlation card (which filters WHERE dag_node_id IS NOT NULL)
            # actually shows DAG-created subtasks. Without this, the card
            # stays empty in production even when DAGs are running.
            subtask = await self._subtask_mgr.create(
                task=augmented,
                frame_type=node.frame_type,
                model=node.model,
                timeout=self._effective_timeout(node),
                metadata={"dag_id": str(dag.id), "node_name": node.name},
                dag_node_id=node.id,
            )
            now = datetime.now(UTC)
            await self._store.update_node(
                node.id,
                status="running",
                subtask_id=subtask.id,
                started_at=now,
                # F064.1: baseline activity ping at launch. Without this, the
                # stall scan sees last_activity_at=NULL and never flags — but
                # we want a node that fails before its first tool call (e.g.
                # bootstrap error) to also surface via the stall path.
                last_activity_at=now,
            )
            node.status = "running"
            node.last_activity_at = now
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
                timeout_seconds=self._effective_timeout(node),
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
