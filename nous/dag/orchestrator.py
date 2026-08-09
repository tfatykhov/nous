"""F038: DAG Orchestrator — state machine that advances DAGs on each tick.

F087 invariants — three interacting lifetimes
---------------------------------------------
Most of the durability bugs found while building F087 came from confusing
these. Written down because the set of mutation sites is not obvious from any
single function, and each was discovered separately during review.

1. **DAG outcome (generation).** `execution_dags.delivery_generation` names
   WHICH terminal outcome a delivery is for. `retry_node` and `cancel_dag` do
   NOT take `self._lock`, so a retry can reactivate — and even re-complete —
   a DAG while `deliver()` is awaiting Telegram or a 120 s summary turn.
   Every delivery write (`mark_delivered`, `save_delivery_summary`,
   `bump_delivery_attempt`) is therefore fenced on the generation observed
   when the sweep loaded the DAG. Fencing on "terminal and undelivered" alone
   is NOT sufficient: a fast retry makes that predicate true again.

2. **Node attempt (`tokens_counted`).** The token claim is one-shot and PER
   ATTEMPT. Exactly three sites reset it, and each must call
   `_account_before_retry` FIRST, because the relaunch replaces `subtask_id`
   and an unbanked attempt then becomes unreachable forever:
     - `retry_node` — the directly retried node
     - `retry_node` — the selectively-unblocked downstream nodes
     - `_try_fix_failed_nodes` — the fix-stage retry branch

   When banking FAILS, ONLY the operator-initiated `retry_node` refuses (it
   raises, and the operator simply tries again). Every AUTOMATIC path —
   downstream descendants and the fix stage — proceeds with an ERROR log.
   Refusing on an automatic path trades a bookkeeping loss for a permanent
   availability loss: a descendant would be stranded, and the fix stage would
   defer every tick forever, leaving the parent 'failed' and the fix node
   non-terminal so `_check_dag_completion` could never finalize the DAG. Both
   are the exact failure class this change exists to remove, and both are
   strictly worse than an under-counted token total.

3. **Subtask settlement.** A node reaching a terminal status does NOT mean its
   subtask has. `cancel_dag`, failure propagation and the reaper all
   terminalize a node while the worker may still run (`SubtaskManager.cancel`
   bounds the leak, it does not preempt). Claims are only finalized for
   subtasks in `_SETTLED_SUBTASK_STATUSES`; anything else stays retryable.

A recurring lesson across all three: two consecutive commits are a crash
window. Prefer one transaction, or a sweep that can resume from the
intermediate state.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from nous.config import Settings
from nous.dag._workspace import assert_inside_root, compute_workspace_path
from nous.dag.store import _TERMINAL_DAG_STATUSES, DAGStore
from nous.heart.subtasks import SubtaskQueueFull
from nous.heartbeat.dynamic import DynamicCheckLimitReached
from nous.storage.models import DAGNode, ExecutionDAG

if TYPE_CHECKING:
    from nous.dag.delivery import DAGResultDelivery
    from nous.events import EventBus
    from nous.heart.subtasks import SubtaskManager
    from nous.heartbeat.dynamic import DynamicCheckLoader

logger = logging.getLogger(__name__)

# Terminal statuses — nodes in these states won't be touched.
# F066.1: 'skipped' is a terminal state introduced by skip_and_continue
# fix action; it behaves like 'completed' for dependency resolution but
# is distinguished in telemetry.
_TERMINAL = frozenset({"completed", "failed", "blocked", "cancelled", "skipped"})

# F066.1: statuses that "resolve" a node for dependency-resolution
# purposes — i.e. _find_ready_nodes treats them as satisfied predecessors.
# `skipped` joins `completed` here because skip_and_continue says
# "proceed past this failure"; cascade-failed nodes are NOT in this set.
_RESOLVED = frozenset({"completed", "skipped"})

# Budget warning threshold (80%)
_BUDGET_WARNING_RATIO = 0.80

# Marker written to DAGNode.error when budget enforcement cancels a node.
# Load-bearing: _handle_budget_exceeded reads it back on later ticks to tell
# "enforcement already curtailed work on this DAG" from "nothing to curtail".
_BUDGET_CANCEL_ERROR = "Token budget exceeded"

# Subtask states whose token counters will never change again, so a one-shot
# claim over them is safe. 'cancelled' qualifies because SubtaskManager.cancel
# makes the row terminal and the worker's eventual complete()/fail() is a
# no-op against the terminal guard — the persisted counters are the last word,
# even though a cancelled worker's final usage was never flushed.
_SETTLED_SUBTASK_STATUSES = frozenset({"completed", "failed", "cancelled"})

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
        llm_client: object | None = None,
        delivery: DAGResultDelivery | None = None,
    ) -> None:
        self._store = store
        self._subtask_mgr = subtask_mgr
        self._dynamic_loader = dynamic_loader
        self._bus = bus
        self._settings = settings
        # F066.1 Phase 1.5: optional LLM client for fix-node free-form dispatch.
        # When None or when settings.dag_fix_llm_dispatch_enabled=False, the
        # orchestrator falls back to fix_executor.choose_action (rule-based,
        # Phase 1 behavior).
        self._llm_client = llm_client
        # F087: optional result-delivery collaborator. None disables the
        # sweep entirely (the orchestrator still runs, DAGs still finish —
        # they just aren't announced).
        self._delivery = delivery
        # F087: set True by whoever installs the tick. Explicit rather than
        # inferred from last_tick_at, which would false-negative during the
        # first tick interval. dag_create refuses when this is False so the
        # agent never creates a DAG that can never advance.
        self.clock_wired = False
        self.last_tick_at: datetime | None = None
        self._lock = asyncio.Lock()
        # F087 (@codex P2 on 9c8cf74): the delivery sweep does slow EXTERNAL
        # I/O — a Telegram round-trip, and up to
        # dag_delivery_agent_summary_timeout_seconds per DAG for an authored
        # summary, times dag_delivery_batch_size DAGs. It must not sit inside
        # `_lock`, which exists to serialize fast state-machine work: holding
        # that for minutes blocks start_dag(), so dag_create hangs after
        # inserting its DAG, and stalls the whole tick loop. Deliveries still
        # serialize against each other on their own lock so a DAG cannot be
        # announced twice by overlapping sweeps.
        self._delivery_lock = asyncio.Lock()
        # Strong reference to the detached sweep so it cannot be GC'd mid-flight.
        self._delivery_task: asyncio.Task | None = None
        # Audit DG-4 (review P2): in-memory per-node deferral counter so a node
        # that keeps hitting a saturated subtask/check pool can't bounce
        # ready<->pending forever invisibly (a perpetually-pending node never
        # enters 'running', so stall detection never fires). After the cap it
        # fails with a clear error. Counts reset on a successful launch and on
        # process restart (a benign backstop reset).
        self._defer_counts: dict = {}

    # Backstop: ~this many consecutive deferrals (ticks) of a saturated pool
    # before a node is failed rather than deferred again.
    _MAX_DEFERRALS = 30

    async def _defer_node(self, node: DAGNode, dag: ExecutionDAG, reason: str) -> None:
        """Demote a node to 'pending' on transient resource saturation, with a
        backstop cap (DG-4 / review P2) so a never-draining pool surfaces as a
        failure instead of an invisible infinite ready<->pending bounce."""
        count = self._defer_counts.get(node.id, 0) + 1
        self._defer_counts[node.id] = count
        if count >= self._MAX_DEFERRALS:
            self._defer_counts.pop(node.id, None)
            await self._store.update_node(
                node.id,
                status="failed",
                error=f"{reason} — still saturated after {count} deferrals",
            )
            node.status = "failed"
            logger.warning(
                "DAG %s node %s FAILED after %d deferrals: %s",
                dag.id, node.name, count, reason,
            )
            return
        await self._store.update_node(node.id, status="pending")
        node.status = "pending"
        log = logger.warning if count >= 10 else logger.info
        log(
            "Deferring node %s in DAG %s (attempt %d) — %s; retry next tick",
            node.name, dag.id, count, reason,
        )

    async def tick(self) -> int:
        """Advance all active DAGs. Returns number of DAGs processed.

        Protected by a lock to prevent concurrent tick races.
        """
        async with self._lock:
            self.last_tick_at = datetime.now(UTC)
            dags = await self._store.get_active_dags()
            for dag in dags:
                try:
                    await self._advance_dag(dag)
                except Exception:
                    logger.exception("Error advancing DAG %s", dag.id)

        # F087: drain terminal-but-undelivered DAGs. Deliberately outside the
        # per-DAG loop above, which only sees pending/running rows — a DAG that
        # reached terminal on an earlier tick (or before a restart) is picked
        # up here and nowhere else.
        #
        # Also deliberately OUTSIDE `_lock` (@codex P2 on 9c8cf74): this does
        # slow external I/O, and holding the state-machine lock across it made
        # dag_create hang and stalled the tick loop. `_delivery_lock` keeps
        # sweeps from overlapping each other without blocking anything else;
        # when one is still in flight the next tick simply skips it rather
        # than queueing ticks behind a two-minute summary turn.
        # @codex P2 on ad857d0: releasing `_lock` was not enough. HeartbeatRunner
        # ._loop does `await self.dag_orchestrator.tick()`, so awaiting the sweep
        # HERE still stalls every later heartbeat phase — DAG advancement,
        # reaping, dynamic check sync, digest, tuning — for as long as the
        # external I/O takes. The sweep is detached instead: the tick returns as
        # soon as the state machine is done, and delivery proceeds on its own
        # task. `_delivery_lock` still prevents overlapping sweeps, so a DAG is
        # never announced twice.
        if self._delivery_lock.locked():
            logger.debug("F087: delivery sweep already in flight — skipping")
        else:
            # Keep a reference: a bare create_task can be garbage-collected
            # mid-flight, which would silently drop the notification.
            self._delivery_task = asyncio.create_task(self._run_delivery_sweep())

        return len(dags)

    async def _run_delivery_sweep(self) -> None:
        """Body of the detached sweep. Never raises into the task."""
        async with self._delivery_lock:
            try:
                await self._deliver_terminal_dags()
            except Exception:
                logger.exception("F087: DAG delivery sweep failed")

    async def wait_for_delivery(self) -> None:
        """Await the in-flight detached sweep, if any.

        For tests and orderly shutdown — the tick loop itself deliberately
        never waits on this.
        """
        task = self._delivery_task
        if task is not None and not task.done():
            await task

    # ------------------------------------------------------------------
    # F087: durable result delivery
    # ------------------------------------------------------------------

    async def _deliver_terminal_dags(self) -> None:
        """Deliver results for DAGs that finished but were never announced.

        Reaching terminal and being delivered are separate transitions, so a
        process that dies between them re-delivers here on the next tick.
        That makes delivery at-least-once rather than best-effort.

        Attempts are bounded: once a DAG has burned
        ``dag_delivery_max_attempts``, it is marked delivered with
        ``delivery_error`` set so the sweep stops retrying it forever, while
        the failure stays visible on the row instead of vanishing.
        """
        if not self._settings.dag_result_delivery_enabled:
            return
        if self._delivery is None:
            return

        dags = await self._store.get_undelivered_terminal_dags(
            limit=self._settings.dag_delivery_batch_size
        )
        for dag in dags:
            # Pin the outcome we are about to deliver. Every delivery write is
            # fenced on this, so if retry_node reactivates (and even re-
            # completes) the DAG while we await Telegram or a summary turn,
            # none of our writes can land on the newer outcome (@codex P1 on
            # e94cd42).
            generation = dag.delivery_generation

            # @codex P2 on a4e302b: _reconcile_token_accounting is reached only
            # via _advance_dag, and tick() runs that only for pending/running
            # DAGs. If accounting was unavailable on the completion tick,
            # _check_dag_completion terminalized the DAG immediately after and
            # the promised next-tick retry never ran — leaving the total, the
            # budget verdict and the announced summary permanently low. This is
            # the last point before the number is published, so reconcile here
            # too. Cheap: it no-ops once every node carries tokens_counted.
            try:
                accounted = await self._reconcile_token_accounting(dag)
            except Exception:
                logger.warning(
                    "F087: pre-delivery token reconciliation raised for DAG %s",
                    str(dag.id)[:8], exc_info=True,
                )
                accounted = False

            # @codex P2 on 2e27143: delivering leaves the DAG out of every
            # future sweep, so a total published while still incomplete can
            # never be corrected. Defer to give reconciliation another tick —
            # but only while attempts remain, because an unannounced DAG is a
            # worse outcome than a slightly low token count. The final attempt
            # always publishes.
            if not accounted:
                cap = self._settings.dag_delivery_max_attempts
                if dag.delivery_attempts + 1 < cap:
                    await self._record_delivery_failure(
                        dag, generation,
                        "token reconciliation incomplete — deferring",
                    )
                    continue
                logger.warning(
                    "F087: delivering DAG %s with an incomplete token total "
                    "after %d attempts — the notification matters more",
                    str(dag.id)[:8], dag.delivery_attempts,
                )

            try:
                outcome = await self._delivery.deliver(dag)
            except Exception as exc:
                # deliver() is written not to raise, so reaching here means a
                # genuine bug rather than a failed leg. Treat it as an
                # attempt so a persistent crash still hits the cap.
                logger.exception(
                    "F087: delivery raised for DAG %s", str(dag.id)[:8]
                )
                await self._record_delivery_failure(
                    dag, generation, f"delivery error: {exc}"
                )
                continue

            # Bank an agent-authored summary BEFORE recording the outcome, so
            # a crash in between still leaves the expensive LLM turn paid for
            # exactly once (@codex P2 on da5dc06).
            if outcome.summary_authored and outcome.summary:
                try:
                    await self._store.save_delivery_summary(
                        dag.id, generation, outcome.summary
                    )
                except Exception:
                    logger.warning(
                        "F087: could not cache delivery summary for DAG %s — "
                        "a retry will regenerate it",
                        str(dag.id)[:8], exc_info=True,
                    )

            if outcome.delivered:
                if await self._store.mark_delivered(dag.id, generation):
                    logger.info(
                        "F087: delivered DAG %s (%s) via %s",
                        str(dag.id)[:8],
                        dag.status,
                        ", ".join(leg.name for leg in outcome.legs if leg.ok)
                        or "no legs",
                    )
                else:
                    # Fence rejected the write: retry_node reactivated this DAG
                    # while deliver() was awaiting. The notification for the
                    # PREVIOUS outcome already went out, which is correct; the
                    # retry will be announced on its own when it terminalizes.
                    logger.info(
                        "F087: DAG %s was reactivated mid-delivery — the "
                        "previous outcome was announced, the retry will be "
                        "announced separately",
                        str(dag.id)[:8],
                    )
            else:
                await self._record_delivery_failure(
                    dag, generation, outcome.failure_detail
                )

    async def _record_delivery_failure(
        self, dag: ExecutionDAG, generation: int, detail: str
    ) -> None:
        """Bump the attempt counter, giving up loudly once the cap is hit.

        Fenced on `generation` like every other delivery write: a failure
        belonging to a superseded outcome must not burn an attempt on, or
        park a stale error against, the outcome that replaced it.
        """
        attempts = await self._store.bump_delivery_attempt(
            dag.id, generation, detail
        )
        if attempts == 0:
            # Fence rejected it — this delivery was superseded mid-flight.
            logger.info(
                "F087: discarding a delivery failure for superseded DAG %s "
                "outcome (generation %d): %s",
                str(dag.id)[:8], generation, detail,
            )
            return
        cap = self._settings.dag_delivery_max_attempts
        if attempts >= cap:
            await self._store.mark_delivered(
                dag.id, generation,
                error=f"gave up after {attempts} attempts — {detail}",
            )
            logger.error(
                "F087: giving up on delivering DAG %s after %d attempts — %s",
                str(dag.id)[:8], attempts, detail,
            )
        else:
            logger.warning(
                "F087: delivery attempt %d/%d failed for DAG %s — %s",
                attempts, cap, str(dag.id)[:8], detail,
            )

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

        # Don't cancel already-terminal DAGs.
        # @codex P2 on 2ccc026: 'partial' was missing here, and F087 makes that
        # omission consequential. partial IS terminal — it is in the delivery
        # sweep's domain — so cancelling one rewrote a terminal outcome without
        # bumping delivery_generation or clearing delivery state. An in-flight
        # delivery would then satisfy the generation fence and mark the newly
        # cancelled outcome delivered using the PARTIAL notification; and if
        # partial had already been delivered, the cancellation was never
        # announced at all.
        if dag.status in _TERMINAL_DAG_STATUSES:
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
        # F087: only 'failed'/'partial' DAGs are reactivated below, and
        # get_active_dags() serves only pending/running — so retrying a node
        # in a CANCELLED DAG used to report success while leaving the node
        # 'pending' in a DAG the tick loop never advances again. Refuse before
        # mutating anything. Reactivating instead would also resurrect the
        # downstream subtree via the selective-unblock loop, which is a
        # bigger semantic change than "retry this one node" asks for.
        if dag.status == "cancelled":
            raise ValueError(
                f"DAG {dag_id} is cancelled — retrying a node would leave it "
                "pending in a DAG that never advances. Cancellation is "
                "deliberate; create a new DAG instead."
            )

        # Bank the old attempt's tokens BEFORE clearing its claim and dropping
        # subtask_id below — afterwards they are unrecoverable (@codex P2).
        # Refuse the retry rather than lose them: this is an operator action,
        # and the failures that reach here are transient, so "try again" is a
        # far better outcome than a silently under-counted DAG.
        if not await self._account_before_retry(node, dag):
            raise ValueError(
                f"Could not record the previous attempt's token usage for "
                f"'{node_name}'. Retrying now would lose it — try again in a "
                "moment."
            )

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
            # @codex P2 on b3c78c3: the token claim is PER ATTEMPT. Leaving it
            # set means the replacement subtask's terminal sync loses the
            # claim race against its own predecessor and silently adds none of
            # the retry's usage — permanently under-reporting tokens_consumed
            # and letting later waves run past an enforced budget.
            tokens_counted=False,
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
                # tokens_counted reset for the same per-attempt reason as the
                # retried node above — a cancel_cascade node may have been
                # running (and consuming) when it was cancelled.
                # @codex P2 on d8b68bf: and for the same reason, its old
                # attempt must be banked FIRST. This is the third of the three
                # reset sites; the previous commit covered only two.
                if not await self._account_before_retry(n, dag):
                    # Descendants differ from the directly-retried node: there
                    # is no automatic second chance for them, so refusing here
                    # would STRAND a recoverable node permanently — trading a
                    # bookkeeping loss for an availability loss, in a change
                    # whose whole purpose is removing silent dead ends.
                    # Unblock anyway and make the loss loud instead of silent,
                    # which is the substance of the @codex objection.
                    logger.error(
                        "F087: unblocking node %s WITHOUT banking its previous "
                        "attempt's tokens — that usage is lost from the DAG "
                        "total, but stranding the node would be worse",
                        n.name,
                    )
                await self._store.update_node(
                    n.id, status="pending", error=None, tokens_counted=False
                )

        # Reactivate DAG if it was marked failed.
        # Status and delivery state are ONE transition (@codex P2 on 9ca8fcd):
        # a crash between a committed 'running' and a separate delivery reset
        # leaves a live DAG carrying a stale delivered_at, which finishes
        # normally and is then silently never announced.
        if dag.status in ("failed", "partial"):
            await self._store.reactivate_for_retry(dag_id)

    # ------------------------------------------------------------------
    # Internal: DAG advancement
    # ------------------------------------------------------------------

    async def _advance_dag(self, dag: ExecutionDAG) -> None:
        """Core state machine: sync → stall → budget → failures → launch → complete."""
        # 1. Sync node statuses from underlying primitives
        await self._sync_node_statuses(dag)

        # 1.5 Poll awaiting_check nodes
        await self._poll_awaiting_checks(dag)

        # 1.55 F087: retry accounting for terminal nodes whose roll-up failed
        # transiently. Must precede the budget check below so a recovered
        # count is visible to enforcement on this very tick.
        await self._reconcile_token_accounting(dag)

        # 1.6 F087: wall-clock backstop. Runs before stall detection because
        # it is the coarser, unconditional bound — a node past its own
        # timeout plus grace is failed regardless of whether activity pings
        # were ever wired.
        if self._settings.dag_node_reaper_enabled:
            await self._reap_overrun_nodes(dag)

        # 1.7 F064.1: Stall detection. Runs after sync (so just-completed
        # nodes are no longer 'running') and before failure-propagation
        # (so a node marked stalled here cascades on the same tick).
        if self._settings.dag_stall_detection_enabled:
            await self._check_stalled_nodes(dag)

        # 2. Check token budget.
        # F087: tokens_consumed was structurally always 0 until the accounting
        # wiring in _sync_subtask_node landed, so this branch had never once
        # executed in production. Enforcement is therefore gated separately
        # from accounting — flipping both at once would start cancelling DAGs
        # for anyone who had set token_budget casually. The warning log runs
        # either way so operators can size budgets before enabling the cancel.
        if dag.token_budget:
            ratio = dag.tokens_consumed / dag.token_budget
            if ratio >= 1.0:
                if self._settings.dag_token_budget_enforcement_enabled:
                    logger.warning("DAG %s exceeded token budget", dag.id)
                    # Only stop advancing when enforcement actually took over.
                    # If it declined (nothing left to curtail), fall through so
                    # _check_dag_completion still finalizes the DAG — otherwise
                    # it would sit 'running' forever.
                    if await self._handle_budget_exceeded(dag):
                        return
                logger.warning(
                    "DAG %s exceeded token budget (%d/%d) — enforcement "
                    "disabled, letting it run",
                    dag.id, dag.tokens_consumed, dag.token_budget,
                )
            elif ratio >= _BUDGET_WARNING_RATIO:
                logger.info(
                    "DAG %s at %.0f%% of token budget", dag.id, ratio * 100
                )

        # 2.5. Recover orphaned wave-0 nodes that are stuck in 'ready' status
        # because start_dag() was bypassed or the dispatch failed silently.
        await self._recover_stale_ready_nodes(dag)

        # 2.7. F066.1: try to apply a fix to any node that just transitioned
        # to 'failed' BEFORE the cascade fires. A successful fix either
        # retries the parent (back to 'pending') or skips it (terminal
        # 'skipped') — in either case the cascade should not propagate.
        await self._try_fix_failed_nodes(dag)

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
                await self._sync_subtask_node(node, dag)
            elif node.node_type == "check" and node.check_name:
                await self._sync_check_node(node)

    async def _account_before_retry(self, node: DAGNode, dag: ExecutionDAG) -> bool:
        """F087: bank the OLD attempt's tokens before its claim is reset.

        @codex P2 on e94cd42: both retry paths clear `tokens_counted` and then
        replace `subtask_id` with the retry's row (retry_node nulls it; the
        fix-stage relaunch overwrites it). If the previous attempt was never
        accounted — because its roll-up failed transiently, or because the
        subtask had not settled yet — reconciliation afterwards can only ever
        see the REPLACEMENT subtask, so the first attempt's tokens are lost
        from both the node and the DAG total for good.

        Returns whether it is SAFE to proceed with the reset.

        @codex P2 on 954ecce: this was previously best-effort — it returned
        without confirming, while callers cleared the claim and replaced
        `subtask_id` regardless, so a transient lookup or claim failure lost
        the attempt permanently. "Bookkeeping must not block the retry" was
        the wrong trade: silent permanent loss is worse than a retry the
        operator can simply issue again. Callers now refuse to reset on False.

        A MISSING subtask row returns True: that usage is genuinely
        unrecoverable rather than transiently unavailable, so blocking would
        wedge the retry forever with nothing to gain.
        """
        if node.tokens_counted or not node.subtask_id or not self._subtask_mgr:
            return True
        try:
            subtask = await self._subtask_mgr.get(node.subtask_id)
            if subtask is None:
                return True
            if subtask.status not in _SETTLED_SUBTASK_STATUSES:
                # Not settled yet — its counters could still change, so
                # claiming now would freeze the wrong value (invariant 3).
                # Blocking the reset keeps the attempt reachable.
                logger.warning(
                    "F087: refusing to reset node %s — its previous subtask is "
                    "still %s, so that attempt's tokens are not final yet",
                    node.name, subtask.status,
                )
                return False
            await self._count_node_tokens(node, subtask, dag)
            # _count_node_tokens swallows its errors, so the flag is the only
            # honest confirmation that the claim actually landed.
            return node.tokens_counted
        except Exception:
            logger.warning(
                "F087: could not bank tokens for the previous attempt of node "
                "%s — refusing to reset so the attempt stays reachable",
                node.name, exc_info=True,
            )
            return False

    async def _reconcile_token_accounting(self, dag: ExecutionDAG) -> bool:
        """F087: re-attempt token accounting for terminal, unaccounted nodes.

        @codex P2 on 9ca8fcd: `_count_node_tokens` deliberately swallows its
        exceptions so a bookkeeping failure can't knock a node out of its
        status sync — but `_sync_subtask_node` then terminalizes the node, and
        later ticks only sync nodes still in 'running'. A single transient DB
        error therefore stranded that node's tokens permanently, under-
        reporting `tokens_consumed` and weakening budget enforcement.

        This sweep closes that hole: any terminal subtask node that still
        carries `tokens_counted=False` gets another attempt each tick. The
        claim is idempotent, so a node that already succeeded is skipped by
        the flag rather than re-added, and the sweep goes quiet once every
        node is accounted.

        Returns True when every terminal node is accounted for. The delivery
        sweep uses that to avoid publishing a total it knows is incomplete
        (@codex P2 on 2e27143) — a delivered DAG leaves the sweep permanently,
        so an announcement made over a half-counted total can never be
        corrected.
        """
        if not self._subtask_mgr:
            return True
        complete = True
        for node in dag.nodes:
            if node.status not in _TERMINAL or node.tokens_counted:
                continue
            if node.node_type != "subtask" or not node.subtask_id:
                continue  # gate/callback/fix nodes never consume tokens
            try:
                subtask = await self._subtask_mgr.get(node.subtask_id)
            except Exception:
                logger.debug(
                    "F087: token reconciliation could not load subtask for "
                    "node %s; will retry next tick", node.name,
                )
                complete = False
                continue
            if subtask is None:
                # The subtask row is gone, so its usage is unknowable. Claim
                # with zero to stop retrying forever rather than leave the
                # sweep spinning on every tick for the life of the DAG.
                await self._count_node_tokens(
                    node, SimpleNamespace(tokens_in=0, tokens_out=0), dag
                )
            elif subtask.status in _SETTLED_SUBTASK_STATUSES:
                await self._count_node_tokens(node, subtask, dag)
            else:
                # @codex P2 on fa988e7: the node is terminal but its subtask
                # is still pending/running — cancel_dag, failure propagation
                # and the reaper all mark a node terminal while the worker may
                # still be executing (SubtaskManager.cancel bounds the leak,
                # it does not preempt). Claiming now would freeze the CURRENT
                # counters as final and, because the claim is one-shot, no
                # later value could ever replace them. Leave it retryable.
                logger.debug(
                    "F087: node %s is terminal but its subtask is %s — "
                    "deferring the token claim until it settles",
                    node.name, subtask.status,
                )
                complete = False
                continue
            # _count_node_tokens swallows its own errors, so the flag it sets
            # is the only honest signal that the claim actually landed.
            if not node.tokens_counted:
                complete = False
        return complete

    async def _sync_subtask_node(
        self, node: DAGNode, dag: ExecutionDAG | None = None
    ) -> None:
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

        # F087: roll the subtask's token usage into the DAG total. Keyed on
        # the SUBTASK reaching terminal rather than the node, so the
        # awaiting_check path (subtask done, node still polling a shell
        # command) is counted too — its tokens are already final. One call
        # site covers all four transitions below.
        # ``dag`` is None only when a caller invokes this helper directly
        # (tests); the tick path always threads it so the in-memory total
        # stays consistent with the row for the budget check later this tick.
        if subtask.status in ("completed", "failed") and dag is not None:
            await self._count_node_tokens(node, subtask, dag)

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

    async def _count_node_tokens(
        self, node: DAGNode, subtask: object, dag: ExecutionDAG
    ) -> None:
        """F087: add a finished subtask's tokens to its DAG's running total.

        `DAGStore.update_dag_tokens` existed since F038 but had no production
        caller, so `tokens_consumed` was structurally always 0 and the budget
        branch in `_advance_dag` could never fire. This is that missing call.

        `_sync_subtask_node` re-runs for the same node across ticks, so the
        claim and the roll-up happen together in
        `claim_and_add_node_tokens` — one transaction, so a crash between
        them cannot leave a node marked counted with nothing added (@codex P2
        on da5dc06).

        The in-memory `dag.tokens_consumed` is refreshed too. `_advance_dag`
        loaded `dag` BEFORE this sync ran, so without this the budget check
        later in the very same tick would read a stale total and dispatch the
        next wave despite the DAG having just gone over (@codex P2).

        Never raises: token accounting is bookkeeping, and losing a count
        must not knock a node out of its status sync.
        """
        try:
            tokens = int(getattr(subtask, "tokens_in", 0) or 0) + int(
                getattr(subtask, "tokens_out", 0) or 0
            )
            # Pin the claim to the attempt we actually read (@codex P2 on
            # ad857d0): a concurrent retry_node may have already banked this
            # attempt, reset the flag and swapped subtask_id, and re-claiming
            # from our stale snapshot would double-add it.
            claimed = await self._store.claim_and_add_node_tokens(
                node.id, node.dag_id, tokens,
                expected_subtask_id=node.subtask_id,
            )
            if not claimed:
                return  # already counted on an earlier tick
            node.tokens_counted = True
            node.tokens_used = tokens
            dag.tokens_consumed = (dag.tokens_consumed or 0) + tokens
        except Exception:
            logger.exception(
                "F087: token accounting failed for node %s", node.name
            )

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

    async def _reap_overrun_nodes(self, dag: ExecutionDAG) -> None:
        """F087: fail running nodes that blew past their wall-clock budget.

        Timeout enforcement is otherwise delegated entirely to the underlying
        primitive: `_launch_subtask_node` hands `_effective_timeout(node)` to
        the subtask and the DAG node itself never checks elapsed time. That
        holds while the subtask is alive, but a subtask orphaned by a crash
        (`reclaim_stale` runs once at worker start and only touches rows
        already past timeout) leaves the node 'running' forever — which keeps
        its DAG 'running' forever and permanently consumes one of the
        MAX_ACTIVE_DAGS slots. Five of those and dag_create is bricked.

        The grace period exists so this never preempts the primitive's own,
        richer error: the subtask executor gets `timeout` seconds to fail the
        node itself, and only if it is still 'running' `grace` seconds later
        do we conclude nobody is coming.

        Mirrors F064.1's ordering: tear the primitive down BEFORE marking the
        node failed, so a still-live subtask stops burning tokens and holding
        a worker slot rather than running on untracked.
        """
        now = datetime.now(UTC)
        grace = self._settings.dag_node_timeout_grace_seconds
        for node in dag.nodes:
            if node.status != "running":
                continue
            started = node.started_at
            if started is None:
                # Launched without a timestamp — wall-clock is unknowable, so
                # leave it to stall detection rather than guess.
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            budget = self._effective_timeout(node) + grace
            elapsed = (now - started).total_seconds()
            if elapsed <= budget:
                continue

            # node.started_at is set at DISPATCH, but a subtask sits 'pending'
            # until a worker dequeues it. With NOUS_SUBTASK_WORKERS=2 and a
            # five-deep pending queue, a legitimate task can wait past
            # timeout+grace before running a single turn.
            #
            # The dispatch clock is therefore only an upper bound on real
            # execution time — useful as a cheap pre-filter (subtask.started_at
            # is always >= node.started_at, so a node inside budget by the
            # dispatch clock is certainly inside it by the execution clock),
            # but never as the decision.
            #
            # @codex P2 on 2ccc026 spared 'pending' subtasks; @codex P2 on
            # 7cc6ae0 caught that this was a half-measure — the moment a worker
            # dequeues a long-queued task its status flips to 'running' while
            # node.started_at still holds the dispatch time, so the very next
            # tick reaped work that had just begun. Measure from the execution
            # clock instead of adding another status guard.
            if node.node_type == "subtask" and node.subtask_id and self._subtask_mgr:
                try:
                    subtask = await self._subtask_mgr.get(node.subtask_id)
                except Exception:
                    # @codex P2 on ab0ff44: swallowing this to `None` fell
                    # through and reaped on the DISPATCH clock — re-creating
                    # the exact false positive the execution-clock check
                    # exists to prevent, on nothing worse than a transient DB
                    # blip. Reaping is destructive and this backstop is never
                    # urgent to the second, so defer to the next tick instead
                    # of deciding from a clock we know is wrong.
                    logger.warning(
                        "F087: could not read the execution clock for node %s "
                        "— deferring the reap rather than deciding from the "
                        "dispatch clock",
                        node.name, exc_info=True,
                    )
                    continue
                if subtask is not None:
                    if subtask.status == "pending":
                        logger.debug(
                            "F087: node %s is past its dispatch-clock budget "
                            "but its subtask is still queued — not reaping",
                            node.name,
                        )
                        continue
                    exec_started = getattr(subtask, "started_at", None)
                    if exec_started is not None:
                        if exec_started.tzinfo is None:
                            exec_started = exec_started.replace(tzinfo=UTC)
                        exec_elapsed = (now - exec_started).total_seconds()
                        if exec_elapsed <= budget:
                            logger.debug(
                                "F087: node %s is past its dispatch-clock "
                                "budget but has only been executing %.0fs — "
                                "not reaping",
                                node.name, exec_elapsed,
                            )
                            continue
                        elapsed = exec_elapsed

            error_msg = (
                f"exceeded wall-clock budget: running {elapsed:.0f}s "
                f"(timeout {self._effective_timeout(node)}s + grace {grace}s)"
            )
            logger.warning(
                "F087: reaping node %s (dag %s) — %s",
                node.name, dag.id, error_msg,
            )
            await self._cancel_node(node)

            # @codex P2 on ac9da7f: the subtask can complete between the status
            # read above and this cancel. SubtaskManager.cancel refuses to
            # touch an already-terminal row, so in that window the work
            # actually SUCCEEDED and its result is persisted — overwriting the
            # node as failed would discard a real result and could fail the
            # whole DAG on a technicality. Terminal nodes are no longer
            # re-synced, so this is the last chance to notice. Re-read and
            # hand it to the normal completion path instead.
            #
            # @codex P2 on ab0ff44: this must cover a concurrent FAILURE too,
            # not just a completion. A subtask that goes running -> failed in
            # the same window is equally terminal, cancel() equally declines to
            # touch it, and falling through replaced its real error with the
            # generic wall-clock message — losing the actual reason permanently,
            # since terminal nodes are never re-synced. The distinction that
            # matters is WHO ended it: a row that reached completed/failed on
            # its own carries the truth, whereas 'cancelled' is the reaper's own
            # doing and the wall-clock error is the honest description there.
            if node.node_type == "subtask" and node.subtask_id and self._subtask_mgr:
                try:
                    settled = await self._subtask_mgr.get(node.subtask_id)
                except Exception:
                    settled = None
                if settled is not None and settled.status in ("completed", "failed"):
                    logger.info(
                        "F087: node %s reached '%s' on its own while being "
                        "reaped — keeping the primitive's own outcome instead "
                        "of the wall-clock error",
                        node.name, settled.status,
                    )
                    await self._sync_subtask_node(node, dag)
                    continue

            await self._store.update_node(
                node.id,
                status="failed",
                error=error_msg,
                completed_at=now,
            )
            node.status = "failed"
            node.error = error_msg
            node.completed_at = now

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

    async def _try_fix_failed_nodes(self, dag: ExecutionDAG) -> None:
        """F066.1: apply fix-stage recovery to nodes that just failed.

        For each non-fix node currently in 'failed' status, look up its
        fix child (a fix node with parent_node == this node's name) and,
        if found AND fix_attempts_used < max_fix_attempts, run the fix
        executor and apply the chosen action.

        Action semantics:
          - retry_as_is: bump attempts, set parent back to 'pending' so
            _find_ready_nodes picks it up on the next tick (or this one).
            Clear `error` so cascade doesn't trigger on the stale value.
          - retry_with_amended_prompt: same as retry_as_is in Phase 1
            (no LLM diagnosis → no amended prompt to apply); falls back
            to mark_unrecoverable per fix_executor.choose_action.
          - mark_unrecoverable: leave parent in 'failed'; record fix
            diagnosis in result; let _propagate_failures cascade.
          - skip_and_continue: set parent to 'skipped' (terminal state);
            successors will unblock via _RESOLVED predicate.

        Fail-safe: if the fix node itself errors, we bump
        fix_attempts_used to prevent an infinite retry loop and leave
        the parent in 'failed' so cascade proceeds normally.
        """
        from nous.dag.fix_executor import choose_action, choose_action_llm

        # Index fix nodes by parent_node for O(1) lookup.
        fix_by_parent: dict[str, DAGNode] = {}
        for n in dag.nodes:
            if n.node_type == "fix" and n.parent_node:
                fix_by_parent[n.parent_node] = n

        if not fix_by_parent:
            return

        for parent in dag.nodes:
            if parent.node_type == "fix":
                continue
            if parent.status != "failed":
                continue
            fix_node = fix_by_parent.get(parent.name)
            if fix_node is None:
                continue
            if fix_node.fix_attempts_used >= fix_node.max_fix_attempts:
                continue

            outcome = None
            llm_enabled = (
                getattr(self._settings, "dag_fix_llm_dispatch_enabled", False)
                and self._llm_client is not None
            )
            if llm_enabled:
                # Phase 1.5: try LLM-based dispatch first; fall back to
                # rule-based on any failure (timeout, parse error, etc.).
                try:
                    outcome = await choose_action_llm(
                        parent_name=parent.name,
                        parent_instructions=parent.instructions,
                        parent_error=parent.error,
                        parent_result=parent.result,
                        fix_instructions=fix_node.instructions,
                        fix_actions=fix_node.fix_actions or [],
                        llm_client=self._llm_client,
                        model=getattr(
                            self._settings, "dag_fix_llm_model",
                            "claude-haiku-4-5-20251001",
                        ),
                        timeout_seconds=getattr(
                            self._settings, "dag_fix_llm_timeout_seconds", 10.0,
                        ),
                    )
                    logger.info(
                        "F066.1 Phase 1.5: LLM dispatch chose '%s' for parent '%s'",
                        outcome.action, parent.name,
                    )
                except Exception as exc:
                    logger.warning(
                        "F066.1 Phase 1.5: LLM dispatch failed (%s); falling "
                        "back to rule-based choose_action",
                        type(exc).__name__,
                    )
                    outcome = None

            if outcome is None:
                try:
                    outcome = choose_action(
                        parent_error=parent.error,
                        parent_status=parent.status,
                        fix_actions=fix_node.fix_actions,
                    )
                except Exception:
                    logger.exception(
                        "F066.1: fix_executor raised for parent %s (DAG %s); "
                        "bumping fix_attempts_used and leaving parent failed",
                        parent.name, dag.id,
                    )
                    fix_node.fix_attempts_used += 1
                    await self._store.update_node(
                        fix_node.id, fix_attempts_used=fix_node.fix_attempts_used,
                    )
                    continue

            # F087 (@codex P2 on 954ecce): if this fix is going to re-enqueue
            # the parent, bank its previous attempt's tokens first — the reset
            # clears the claim and the relaunch overwrites subtask_id, after
            # which that usage is unreachable.
            #
            # This path PROCEEDS on failure (ERROR log), unlike retry_node.
            # An earlier version deferred instead, and CI caught the
            # consequence: the fix stage is automatic and re-fires every tick,
            # so a subtask that never settles deferred forever — the parent
            # stayed 'failed', the fix node never reached terminal, and
            # _check_dag_completion could never finalize the DAG. That is a
            # permanent wedge, which is strictly worse than an under-counted
            # token total, and it is the very failure class this change exists
            # to remove. Only the operator-initiated retry_node refuses;
            # automatic paths always make progress.
            if outcome.action in ("retry_as_is", "retry_with_amended_prompt"):
                if not await self._account_before_retry(parent, dag):
                    logger.error(
                        "F087: retrying %s WITHOUT banking its previous "
                        "attempt's tokens — that usage is lost from the DAG "
                        "total, but wedging the DAG would be worse",
                        parent.name,
                    )

            logger.info(
                "F066.1: fix '%s' chose '%s' for parent '%s' — %s",
                fix_node.name, outcome.action, parent.name, outcome.rationale,
            )

            # Always bump fix_attempts_used so we cap retries even if the
            # action ends up being a noop. Also transition the fix node
            # itself to a terminal status — without this, the DAG never
            # completes because _check_dag_completion sees the fix node
            # stuck in 'pending' forever (Codex P1 on b333f78). The fix
            # node is conceptually a one-shot per firing; if the parent
            # fails again and budget remains, the next tick re-fires it
            # via the same code path (the lookup in fix_by_parent is by
            # parent_node string, not by fix node status).
            fix_node.fix_attempts_used += 1
            fix_node.status = "completed"
            fix_node.result = (
                f"Fix-stage chose '{outcome.action}' for parent '{parent.name}': "
                f"{outcome.rationale or ''}".strip()
            )
            await self._store.update_node(
                fix_node.id,
                fix_attempts_used=fix_node.fix_attempts_used,
                status="completed",
                result=fix_node.result,
            )

            if outcome.action == "retry_as_is" or outcome.action == "retry_with_amended_prompt":
                # Re-enqueue parent for dispatch on next tick.
                # (The previous attempt was already banked above, before the
                # fix attempt was consumed.)
                # tokens_counted=False for the same per-attempt reason as
                # retry_node (@codex P2 on b3c78c3) — the fix-stage retry is
                # the automatic sibling of that manual path and was losing the
                # retry's token usage identically.
                update_kwargs: dict[str, object] = {
                    "status": "pending",
                    "error": None,
                    "tokens_counted": False,
                }
                # F066.1 Phase 1.5: when LLM dispatch returns an amended
                # prompt, replace the parent's instructions so the next
                # dispatch sees the revised prompt. Phase 1 (rule-based)
                # always has amended_prompt=None — this branch is a no-op
                # for that path.
                if (
                    outcome.action == "retry_with_amended_prompt"
                    and outcome.amended_prompt
                ):
                    parent.instructions = outcome.amended_prompt
                    update_kwargs["instructions"] = outcome.amended_prompt
                parent.status = "pending"
                parent.error = None
                await self._store.update_node(parent.id, **update_kwargs)
            elif outcome.action == "skip_and_continue":
                parent.status = "skipped"
                parent.result = (
                    f"Fix '{fix_node.name}': {outcome.rationale or 'skipped by fix-stage'}"
                )
                await self._store.update_node(
                    parent.id, status="skipped", result=parent.result,
                )
            else:
                # mark_unrecoverable — leave 'failed' but annotate.
                diag = f"Fix '{fix_node.name}': {outcome.rationale or 'mark_unrecoverable'}"
                existing = parent.result or ""
                parent.result = (existing + "\n" + diag).strip()
                await self._store.update_node(
                    parent.id, result=parent.result,
                )

    def _find_ready_nodes(self, dag: ExecutionDAG) -> list[DAGNode]:
        """Find pending nodes whose all dependency/context_flow predecessors are completed."""
        # Build set of predecessor node_ids per node (dependency + context_flow)
        dep_map: dict[str, set[str]] = {str(n.id): set() for n in dag.nodes}
        for edge in dag.edges:
            if edge.edge_type in ("dependency", "context_flow"):
                dep_map[str(edge.to_node_id)].add(str(edge.from_node_id))

        # Completed node IDs
        # F066.1: 'skipped' resolves dependencies just like 'completed'.
        completed_ids = {str(n.id) for n in dag.nodes if n.status in _RESOLVED}

        ready: list[DAGNode] = []
        for node in dag.nodes:
            if node.status != "pending":
                continue
            # F066.1: fix nodes are NOT dispatched by the normal ready path.
            # They are launched by _try_fix_failed_nodes when their parent
            # transitions to 'failed'. Skipping here prevents the silent
            # no-op fallthrough in _launch_node where node_type='fix' has
            # no branch.
            if node.node_type == "fix":
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
            self._defer_counts.pop(node.id, None)  # launched — clear backstop
            logger.info(
                "Launched subtask %s for node %s in DAG %s",
                subtask.id, node.name, dag.id,
            )
        except SubtaskQueueFull:
            # Audit DG-4 (2026-06-09): transient subtask-queue congestion must
            # DEFER the node, not fail it (mirrors the F064.2 frame-cap
            # deferral). Previously the pending-limit ValueError fell into the
            # generic handler below and permanently failed the node — and
            # cascaded failure to all its dependents — on a momentarily full
            # queue that would have drained as workers completed. The backstop
            # cap in _defer_node converts an endless bounce into a clear failure.
            await self._defer_node(node, dag, "subtask queue full")
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
            self._defer_counts.pop(node.id, None)  # launched — clear backstop
            logger.info(
                "Launched check '%s' for node %s in DAG %s",
                check_name, node.name, dag.id,
            )
        except DynamicCheckLimitReached:
            # Audit DG-4 (review follow-up): the dynamic-check pool being full
            # is transient, exactly like SubtaskQueueFull on the subtask path.
            # Defer the node instead of permanently failing it + its dependents.
            await self._defer_node(node, dag, "dynamic check pool full")
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
        """Check if all nodes are terminal and set final DAG status.

        F066.1: a fix node that never fires (because its parent reached
        a non-failed terminal state) would otherwise sit in 'pending'
        forever, keeping the DAG running. Auto-complete idle fix nodes
        when their parent terminates without ever failing.
        """
        # Sweep pending fix nodes whose parent reached a non-failed terminal.
        node_by_name = {n.name: n for n in dag.nodes}
        for n in dag.nodes:
            if n.node_type != "fix" or n.status != "pending":
                continue
            parent = node_by_name.get(n.parent_node or "")
            if parent is None:
                continue
            if parent.status in ("completed", "skipped", "cancelled"):
                n.status = "completed"
                n.result = (
                    f"Fix-stage not fired — parent '{parent.name}' "
                    f"reached terminal '{parent.status}' without failure."
                )
                await self._store.update_node(
                    n.id, status="completed", result=n.result,
                )

        statuses = {n.status for n in dag.nodes}

        # If any nodes are still non-terminal, DAG is not done
        non_terminal = statuses - _TERMINAL
        if non_terminal:
            return

        # All nodes are terminal — determine final status.
        # Audit DG-1 (2026-06-09): use _RESOLVED ({completed, skipped}) for the
        # success branch, not "completed" only. A DAG that finished via the
        # skip_and_continue success path (some nodes 'skipped', rest
        # 'completed') previously fell through every branch to the "All nodes
        # blocked" failure case and was finalized 'failed' despite succeeding.
        if all(n.status in _RESOLVED for n in dag.nodes):
            skipped = sum(1 for n in dag.nodes if n.status == "skipped")
            summary = (
                "All nodes completed successfully"
                if not skipped
                else f"All nodes resolved ({skipped} skipped via skip_and_continue)"
            )
            await self._store.update_dag_status(
                dag.id, "completed", result_summary=summary
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

    async def _handle_budget_exceeded(self, dag: ExecutionDAG) -> bool:
        """Cancel pending/ready nodes when budget is exceeded.

        Returns True when enforcement took over the DAG's fate (so the caller
        should stop advancing it this tick), False when it declined because
        there was nothing left to curtail (so normal completion must still
        run — otherwise the DAG would sit 'running' forever, never reaching
        _check_dag_completion).

        F087 (@codex P2 on 1bfe1fa): when the overage curtailed NOTHING — no
        node was cancellable and none is still running — this must not force a
        terminal status of its own. Enforcement exists to stop future work, and
        a DAG whose nodes all finished has no future work to stop; labelling it
        'partial' would claim work was skipped when none was. Falling through
        to _check_dag_completion instead also makes the DAG's final status
        independent of WHEN token accounting landed, which is the actual
        inconsistency codex identified: a transient accounting failure that
        recovers in the pre-delivery sweep now yields exactly the same status
        as accounting that succeeded a tick earlier. The overage is not lost —
        it is logged, and the delivery template renders `Tokens: used/budget`
        whenever a budget is set.

        `cancelled_any` had been computed and never read since F038 (a standing
        ruff F841); it is the signal this branch needed all along.

        @codex P2 on 2e27143: "did enforcement curtail anything" is a property
        of the DAG's HISTORY, not of this tick. An over-budget wave with both
        running work and pending successors cancels the successors on tick 1
        and returns to let the running nodes finish; on tick 2 there is nothing
        left to cancel, so a tick-local `cancelled_any` would decline and let
        the DAG be labelled 'cancelled' rather than 'partial' — even though
        enforcement genuinely did curtail work. Nodes already cancelled with
        the budget marker are therefore counted as prior curtailment.
        """
        cancelled_any = any(
            node.status == "cancelled"
            and (node.error or "").startswith(_BUDGET_CANCEL_ERROR)
            for node in dag.nodes
        )
        for node in dag.nodes:
            if node.status in ("pending", "ready", "awaiting_check"):
                await self._store.update_node(
                    node.id, status="cancelled", error=_BUDGET_CANCEL_ERROR
                )
                node.status = "cancelled"
                node.error = _BUDGET_CANCEL_ERROR
                cancelled_any = True

        # If there are still running nodes, let them finish
        has_running = any(n.status == "running" for n in dag.nodes)
        if not cancelled_any and not has_running:
            logger.warning(
                "DAG %s finished over budget (%s/%s) but nothing was left to "
                "curtail — leaving the final status to normal completion",
                dag.id, dag.tokens_consumed, dag.token_budget,
            )
            return False
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
        return True
