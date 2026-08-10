"""F038: DAG Store — CRUD operations for execution DAGs."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, cast, func, select, update
from sqlalchemy import true as sa_true
from sqlalchemy.orm import selectinload

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest
from nous.storage.database import Database
from nous.storage.models import DAGEdge, DAGNode, ExecutionDAG, Subtask

logger = logging.getLogger(__name__)

MAX_ACTIVE_DAGS = 5

# The delivery sweep's domain. Every delivery write is fenced on this set plus
# `delivered_at IS NULL`, because retry_node/cancel_dag do NOT take the
# orchestrator lock and can reactivate a DAG while deliver() is awaiting
# Telegram or a 120s summary turn (@codex P1 on fa988e7).
_TERMINAL_DAG_STATUSES = ("completed", "failed", "partial", "cancelled")

# codex P2 (FINDING 3): `find_dags_by_id_prefix` below matches this prefix
# against `id::text` via SQL LIKE, where `%`/`_` are wildcards. Neither
# character (nor anything but hex digits and hyphens) can appear in a real
# UUID's string form, so validating against this pattern BEFORE the query
# rejects both metacharacters outright — simpler and safer than escaping
# them and passing an `escape=` clause through to `.like()`.
_DAG_ID_PREFIX_RE = re.compile(r"^[0-9a-fA-F-]{1,36}$")


class DAGStore:
    """CRUD operations for DAG orchestration."""

    def __init__(self, database: Database, agent_id: str, settings: Settings) -> None:
        self._db = database
        self._agent_id = agent_id
        self._settings = settings

    async def create(self, request: DAGCreateRequest) -> ExecutionDAG:
        """Create a DAG with nodes and edges from a validated request.

        Wave-0 nodes are set to 'ready' status; all others start 'pending'.
        Raises ValueError if the active DAG limit is reached.
        """
        async with self._db.session() as session:
            # Check active DAG limit
            active_count = await session.scalar(
                select(func.count())
                .select_from(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(["pending", "running"]))
            )
            if active_count >= MAX_ACTIVE_DAGS:
                raise ValueError(
                    f"Active DAG limit reached ({MAX_ACTIVE_DAGS}). "
                    "Cancel or complete existing DAGs first."
                )

            # Compute wave assignments
            waves = request.compute_waves()

            # Create DAG
            dag = ExecutionDAG(
                agent_id=self._agent_id,
                name=request.name,
                description=request.description,
                source=request.source,
                original_request=request.original_request,
                token_budget=request.token_budget,
                # F064.2: per-DAG per-frame-type concurrency caps. NULL when
                # the request omits the field — fully backward compatible.
                max_concurrent_by_frame_type=request.max_concurrent_by_frame_type,
            )
            session.add(dag)
            await session.flush()  # Get dag.id

            # Create nodes
            node_map: dict[str, DAGNode] = {}
            for spec in request.nodes:
                wave = waves.get(spec.name, 0)
                resolved_timeout = min(
                    spec.timeout_seconds if spec.timeout_seconds is not None else self._settings.dag_node_default_timeout,
                    self._settings.dag_node_max_timeout,
                )
                # F064.1: resolve + clamp per-node stall_timeout. None or 0 = disabled.
                # When set, clamp to NOUS_DAG_NODE_MAX_STALL_TIMEOUT and ALSO
                # enforce stall <= resolved_timeout (codex P2-2 fix: the
                # schema-level validator can't see the resolved default
                # because it runs before store.create's clamp pipeline. We
                # check against the resolved value here, raising before any
                # row is inserted — same semantics, late but pre-commit).
                resolved_stall: int | None
                if spec.stall_timeout_seconds == 0:
                    # Explicitly disabled per-node — no inheritance, no check.
                    resolved_stall = 0
                elif spec.stall_timeout_seconds is None:
                    # Per-node unset → inherits global default at runtime
                    # (orchestrator._effective_stall_timeout). Persist None
                    # to preserve the "inherit" semantic, but ALSO validate
                    # the GLOBAL default against this node's wall-clock
                    # timeout. Otherwise a node with timeout_seconds=60 and
                    # global default_stall_timeout=600 silently never
                    # stalls. @codex P2 on dc914be: skipped this check
                    # previously when per-node stall was unset.
                    resolved_stall = None
                    if self._settings.dag_stall_detection_enabled:
                        inherited = self._settings.dag_node_default_stall_timeout
                        if inherited > 0 and inherited > resolved_timeout:
                            raise ValueError(
                                f"Node '{spec.name}': inherited global "
                                f"stall_timeout={inherited} exceeds this node's "
                                f"effective wall-clock timeout {resolved_timeout} — "
                                "stall would never fire (silent dead config). Set "
                                "stall_timeout_seconds=0 on this node to opt out, "
                                "or raise timeout_seconds."
                            )
                else:
                    resolved_stall = min(
                        spec.stall_timeout_seconds,
                        self._settings.dag_node_max_stall_timeout,
                    )
                    if resolved_stall > resolved_timeout:
                        raise ValueError(
                            f"Node '{spec.name}': stall_timeout_seconds="
                            f"{spec.stall_timeout_seconds} exceeds effective "
                            f"wall-clock timeout {resolved_timeout} — stall "
                            "would never fire (silent dead config). Reduce "
                            "stall_timeout_seconds or raise timeout_seconds."
                        )
                # F066.1: fix nodes stay in 'pending' regardless of wave —
                # they only activate when their parent transitions to 'failed'
                # via _try_fix_failed_nodes. Without this guard, a wave-0
                # fix node would be promoted to 'ready' here and dispatched
                # by start_dag's wave_zero loop, hitting the silent
                # fallthrough in _launch_node.
                if spec.type.value == "fix":
                    initial_status = "pending"
                elif wave == 0:
                    initial_status = "ready"
                else:
                    initial_status = "pending"
                node = DAGNode(
                    dag_id=dag.id,
                    name=spec.name,
                    description=spec.description,
                    node_type=spec.type.value,
                    wave=wave,
                    status=initial_status,
                    instructions=spec.instructions or None,
                    tools=spec.tools,
                    frame_type=spec.frame_type,
                    model=spec.model,
                    timeout_seconds=resolved_timeout,
                    completion_condition=spec.completion_condition,
                    completion_check=spec.completion_check,
                    completion_check_interval=spec.completion_check_interval,
                    max_check_attempts=spec.max_check_attempts,
                    stall_timeout_seconds=resolved_stall,
                    # F066.1 — fix-stage columns.
                    parent_node=spec.parent_node,
                    fix_actions=spec.fix_actions,
                    max_fix_attempts=spec.max_fix_attempts,
                    expected_modes=list(spec.expected_modes),
                )
                session.add(node)
                node_map[spec.name] = node

            await session.flush()  # Get node IDs

            # Create edges
            for edge_spec in request.edges:
                edge = DAGEdge(
                    dag_id=dag.id,
                    from_node_id=node_map[edge_spec.from_node].id,
                    to_node_id=node_map[edge_spec.to_node].id,
                    edge_type=edge_spec.edge_type,
                )
                session.add(edge)

            await session.commit()

            # Re-fetch with eager loading to avoid DetachedInstanceError
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.id == dag.id)
                .options(
                    selectinload(ExecutionDAG.nodes),
                    selectinload(ExecutionDAG.edges),
                )
            )
            dag = result.scalar_one()

            logger.info(
                "Created DAG %s (%s) with %d nodes, %d edges",
                dag.id, dag.name, len(dag.nodes), len(dag.edges),
            )
            return dag

    async def get_dag(self, dag_id: UUID) -> ExecutionDAG | None:
        """Fetch a DAG with eager-loaded nodes and edges."""
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .options(
                    selectinload(ExecutionDAG.nodes),
                    selectinload(ExecutionDAG.edges),
                )
            )
            return result.scalar_one_or_none()

    async def get_active_dags(self) -> list[ExecutionDAG]:
        """Get all pending and running DAGs."""
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(["pending", "running"]))
                .options(
                    selectinload(ExecutionDAG.nodes),
                    selectinload(ExecutionDAG.edges),
                )
                .order_by(ExecutionDAG.created_at.asc())
            )
            return list(result.scalars().all())

    async def get_recent_finished_dags(self, limit: int = 20) -> list[ExecutionDAG]:
        """codex P2 on F090.3: `get_recent_dags` orders by `created_at` and
        applies LIMIT before any status filter. `dag_manage action=recent`
        then filtered to finished status in Python — so a DAG that finishes
        AFTER `limit` newer DAGs were created dropped off the created_at
        top-`limit` entirely and was invisible, no matter how recently it
        finished. Long-running DAGs are exactly the ones likely to have
        newer DAGs created while they're still running, so the bug hit
        `recent`'s primary use case (discovering a finished long-running
        DAG). Filters to `_TERMINAL_DAG_STATUSES` and orders by
        `completed_at` in SQL instead, so the limit applies to the
        population the caller actually wants.

        `completed_at` NULLs sort last: every live path to a terminal status
        goes through `update_dag_status`, which stamps `completed_at` in the
        same write whenever `status` moves to one of `_TERMINAL_DAG_STATUSES`
        — a terminal row with a NULL `completed_at` should not exist today.
        Still explicit rather than relying on that invariant never breaking:
        Postgres' DESC default is NULLS FIRST, which would otherwise rank
        such a row as "most recent" instead of the ambiguous case it is.

        Only `nodes` is eager-loaded — the `recent` handler counts nodes but
        never touches `edges`.
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(_TERMINAL_DAG_STATUSES))
                .options(selectinload(ExecutionDAG.nodes))
                .order_by(ExecutionDAG.completed_at.desc().nulls_last())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def find_dags_by_id_prefix(
        self, prefix: str, limit: int = 10
    ) -> list[ExecutionDAG]:
        """codex P2 (FINDING 3): `_resolve_dag`'s old fallback scanned
        `get_active_dags()` then a `get_recent_dags(limit=20)` window — a
        prefix lookup for a finished DAG outside that created_at-bounded
        window (exactly the DAG a user is likely to ask about right after
        an F087 delivery notification announces it) silently returned "not
        found". A prefix is a targeted point-lookup, not a recency query:
        this is a single agent-scoped SQL match across every status and
        age instead.

        Invalid input (anything outside `_DAG_ID_PREFIX_RE`) returns an
        empty list rather than querying — most importantly this rejects
        `%`/`_` before they ever reach `LIKE`, where they are wildcards.

        `limit=10`, not the 2 that would suffice to merely detect
        ambiguity: the caller's `ValueError` lists the matches by id, and a
        human resolving a collision benefits from seeing more than the
        bare minimum. Real prefix collisions at the 8+ hex-char lengths
        this is used with are rare enough that 10 effectively means "all
        of them" without risking an unbounded query on a pathological
        (e.g. single-character) prefix.

        codex P2 (FINDING 7): `_DAG_ID_PREFIX_RE` legitimately admits
        `A`-`F` — case-insensitively-typed hex is still valid UUID
        content — but Postgres renders `id::text` lowercase and `LIKE` is
        case-sensitive there (verified directly: `'2CDE...'::uuid::text
        LIKE '2CDE%'` is `false`, `LIKE lower('2CDE') || '%'` is `true`),
        so an uppercase or mixed-case prefix validated and then matched
        nothing. Not a regression this method introduced — the
        pre-FINDING-3 `str(d.id).startswith(dag_id_str)` had the identical
        failure, since Python's `str(UUID)` is lowercase too — but the
        validator now explicitly documents `A`-`F` as legal input, which
        makes accepting-then-never-matching a worse contract than the old
        code's silent one. Normalized here, not at the call site: a
        caller should not have to know Postgres's rendering to use this
        correctly.
        """
        if not _DAG_ID_PREFIX_RE.fullmatch(prefix):
            return []
        prefix = prefix.lower()
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(cast(ExecutionDAG.id, String).like(f"{prefix}%"))
                .options(selectinload(ExecutionDAG.nodes))
                .order_by(ExecutionDAG.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_active(self) -> int:
        """Count pending + running DAGs."""
        async with self._db.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.status.in_(["pending", "running"]))
            )
            return count or 0

    async def update_dag_status(
        self,
        dag_id: UUID,
        status: str,
        result_summary: str | None = None,
        postmortem: dict | None = None,
    ) -> None:
        """Update DAG status with automatic timestamp management."""
        async with self._db.session() as session:
            values: dict = {"status": status}

            if status == "running":
                values["started_at"] = datetime.now(UTC)
            elif status in ("completed", "failed", "cancelled", "partial"):
                values["completed_at"] = datetime.now(UTC)

            if result_summary is not None:
                values["result_summary"] = result_summary
            if postmortem is not None:
                values["postmortem"] = postmortem

            await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .values(**values)
            )
            await session.commit()

    async def update_node(self, node_id: UUID, **kwargs: object) -> None:
        """Update any fields on a DAG node (agent-scoped)."""
        if not kwargs:
            return
        async with self._db.session() as session:
            await session.execute(
                update(DAGNode)
                .where(DAGNode.id == node_id)
                .where(
                    DAGNode.dag_id.in_(
                        select(ExecutionDAG.id).where(
                            ExecutionDAG.agent_id == self._agent_id
                        )
                    )
                )
                .values(**kwargs)
            )
            await session.commit()

    async def count_running_subtasks_by_frame_type(
        self, dag_id: UUID | None = None
    ) -> dict[str, int]:
        """F064.2: grouped count of IN-FLIGHT subtasks by frame_type.

        Returns a dict {frame_type: count} including the "_default" bucket for
        subtasks whose frame_type is NULL. Used by orchestrator dispatch gating
        to enforce per-frame caps without keeping in-memory state across ticks.

        @codex P1 on aa3c739: caps are configured PER-DAG
        (ExecutionDAG.max_concurrent_by_frame_type), so the count must also be
        per-DAG. Otherwise a concurrent DAG's subtasks consume budget that
        belongs to this DAG. When `dag_id` is set, the count joins through
        DAGNode → ExecutionDAG and restricts to subtasks linked to nodes in
        that DAG. When None (legacy path), counts agent-wide.

        Counts both `status='pending'` AND `status='running'` (@codex P1 on
        c3a4fed): SubtaskManager.create inserts in 'pending'; only the worker
        dequeue transitions to 'running'. Between dispatch and pickup a
        launched subtask is invisible to a 'running'-only count.

        Single grouped SELECT — mirrors heart/subtasks.py:293 count_by_status
        pattern (verified equivalent at codex review time).
        """
        async with self._db.session() as session:
            stmt = (
                select(Subtask.frame_type, func.count())
                .where(Subtask.agent_id == self._agent_id)
                .where(Subtask.status.in_(["pending", "running"]))
            )
            if dag_id is not None:
                # Scope to subtasks linked to nodes in THIS DAG via the
                # F061 PR-3 FK (subtask.dag_node_id → dag_nodes.id).
                stmt = (
                    stmt.join(DAGNode, Subtask.dag_node_id == DAGNode.id)
                    .where(DAGNode.dag_id == dag_id)
                )
            stmt = stmt.group_by(Subtask.frame_type)
            result = await session.execute(stmt)
            out: dict[str, int] = {}
            for frame, count in result.all():
                key = frame if frame is not None else "_default"
                out[key] = int(count)
            return out

    async def touch_node_activity(self, node_id: UUID) -> None:
        """F064.1: Mark a DAGNode as recently active.

        Called by runner._tool_loop on every iteration boundary (top-of-loop,
        before tool dispatch) and by orchestrator._launch_subtask_node at node
        launch. Fire-and-forget — the caller wraps under asyncio.shield so a
        wait_for timeout doesn't cancel an in-flight ping.

        A write failure is logged at DEBUG and swallowed. Stall scan treats
        NULL/stale last_activity_at as not-stalled (wall-clock is the fallback),
        so a dropped ping cannot manufacture a false stall.

        Single UPDATE keyed by primary key — write amplification is bounded
        per the partial index on (last_activity_at) WHERE status='running'.
        """
        try:
            async with self._db.session() as session:
                await session.execute(
                    update(DAGNode)
                    .where(DAGNode.id == node_id)
                    .values(last_activity_at=datetime.now(UTC))
                )
                await session.commit()
        except Exception:
            logger.debug("touch_node_activity failed for node %s", node_id, exc_info=True)

    async def update_dag_tokens(self, dag_id: UUID, tokens: int) -> None:
        """Increment tokens_consumed on a DAG."""
        async with self._db.session() as session:
            await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .values(tokens_consumed=ExecutionDAG.tokens_consumed + tokens)
            )
            await session.commit()

    # ------------------------------------------------------------------
    # F087: durable result delivery
    # ------------------------------------------------------------------

    async def claim_and_add_node_tokens(
        self,
        node_id: UUID,
        dag_id: UUID,
        tokens: int,
        expected_subtask_id: UUID | None = None,
    ) -> bool:
        """F087: claim a node's tokens and roll them up, atomically.

        Flips ``tokens_counted`` false → true, records ``tokens_used``, and
        increments the DAG's ``tokens_consumed`` — all inside ONE transaction,
        and reports whether THIS call won the claim.

        @codex P2 on da5dc06: an earlier version committed the claim in its
        own transaction and only then added to the DAG total. A crash (or a
        failure on either later write) between the two left the node
        permanently marked counted with nothing ever added, and because the
        claim short-circuits every subsequent tick, the DAG total stayed
        silently low forever — which would in turn under-report against
        ``token_budget``. Both writes now commit or neither does.

        The claim is the WHERE clause rather than a read-then-write, so two
        concurrent ticks cannot both observe false and both add.

        @codex P2 on ad857d0: `expected_subtask_id` pins the claim to the
        ATTEMPT the caller actually read. Callers work from a `dag` snapshot,
        and `retry_node` can concurrently bank the old attempt, reset
        `tokens_counted`, and clear `subtask_id`. Without this clause a stale
        caller would re-claim the freshly reset flag using the OLD subtask's
        tokens — double-adding that attempt to the DAG total and leaving the
        flag true so the replacement attempt could never be counted. Pass None
        only when the caller has no attempt identity to pin (it then behaves
        as before).
        """
        async with self._db.session() as session:
            result = await session.execute(
                update(DAGNode)
                .where(DAGNode.id == node_id)
                .where(DAGNode.tokens_counted.is_(False))
                .where(
                    DAGNode.subtask_id == expected_subtask_id
                    if expected_subtask_id is not None
                    else sa_true()
                )
                .where(
                    DAGNode.dag_id.in_(
                        select(ExecutionDAG.id).where(
                            ExecutionDAG.agent_id == self._agent_id
                        )
                    )
                )
                # tokens_used ACCUMULATES rather than overwrites: a retried
                # node really did spend both attempts' tokens, and the DAG
                # total below accumulates the same way. Overwriting would make
                # the node disagree with the DAG it rolls up into.
                .values(
                    tokens_counted=True,
                    tokens_used=DAGNode.tokens_used + tokens,
                )
            )
            if result.rowcount == 0:
                # Already counted on an earlier tick — nothing to add, and
                # nothing to commit.
                await session.rollback()
                return False
            if tokens:
                await session.execute(
                    update(ExecutionDAG)
                    .where(ExecutionDAG.id == dag_id)
                    .where(ExecutionDAG.agent_id == self._agent_id)
                    .values(tokens_consumed=ExecutionDAG.tokens_consumed + tokens)
                )
            await session.commit()
            return True

    async def get_undelivered_terminal_dags(
        self, limit: int = 5
    ) -> list[ExecutionDAG]:
        """F087: DAGs that reached a terminal status but were never delivered.

        This is the sweep's work queue. Because it is a table rather than
        in-memory state, a process that dies between marking a DAG terminal
        and delivering its result picks the DAG back up on the next tick.

        Oldest first so a backlog drains in the order the DAGs finished.
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.delivered_at.is_(None))
                .where(ExecutionDAG.status.in_(_TERMINAL_DAG_STATUSES))
                .options(
                    selectinload(ExecutionDAG.nodes),
                    selectinload(ExecutionDAG.edges),
                )
                .order_by(ExecutionDAG.completed_at.asc().nulls_last())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def mark_delivered(
        self, dag_id: UUID, generation: int, error: str | None = None
    ) -> bool:
        """F087: record that a DAG's result left the box.

        ``error`` is set only on give-up (attempts exhausted): the DAG is
        still marked delivered so the sweep stops retrying, but the reason
        stays on the row instead of disappearing.

        @codex P2 on da5dc06: a successful RETRY must also clear whatever
        ``bump_delivery_attempt`` wrote on the earlier failed attempts.
        Leaving it would produce a row that reports both a successful
        delivery and a delivery error, which contradicts this method's
        contract (error means gave-up) and misleads the dashboard.

        @codex P1 on fa988e7: fenced on the DAG still being terminal AND still
        undelivered. `retry_node` does not take the orchestrator lock, so it
        can reactivate this DAG while `deliver()` is awaiting Telegram or a
        120-second summary turn — and a late blind write would stamp
        `delivered_at` on the now-running DAG, permanently excluding the
        RETRY's outcome from every future sweep. Returns whether the write
        applied so the caller can tell "delivered" from "superseded".

        @codex P1 on e94cd42: that predicate alone loses the FAST-retry race.
        If the retried work terminalizes again before the original await
        returns, the row is terminal and undelivered once more, so the stale
        write succeeds and marks the NEW outcome delivered — which is exactly
        the permanent-exclusion bug the fence was added to prevent, just one
        step later. `generation` pins the write to the specific outcome the
        caller actually delivered.
        """
        async with self._db.session() as session:
            values: dict = {
                "delivered_at": datetime.now(UTC),
                "delivery_error": error,
            }
            result = await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.delivered_at.is_(None))
                .where(ExecutionDAG.status.in_(_TERMINAL_DAG_STATUSES))
                .where(ExecutionDAG.delivery_generation == generation)
                .values(**values)
            )
            await session.commit()
            return result.rowcount > 0

    async def save_delivery_summary(
        self, dag_id: UUID, generation: int, summary: str
    ) -> None:
        """F087: cache an agent-authored summary for reuse across retries.

        @codex P2 on da5dc06: with this absent, a Telegram outage after a
        successful summary leg re-ran a full LLM turn on every sweep — up to
        `dag_delivery_max_attempts` turns and duplicate episodes for a single
        DAG. Written before the delivery outcome is recorded so a crash in
        between still leaves the expensive work banked.

        Fenced like the other delivery writes (@codex P1 on fa988e7): a
        summary authored for the PREVIOUS outcome must not attach itself to a
        DAG that `retry_node` reactivated mid-delivery — the retry would then
        be announced with prose describing the run before it.
        """
        async with self._db.session() as session:
            await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.delivered_at.is_(None))
                .where(ExecutionDAG.status.in_(_TERMINAL_DAG_STATUSES))
                .where(ExecutionDAG.delivery_generation == generation)
                .values(delivery_summary=summary)
            )
            await session.commit()

    async def apply_retry(
        self,
        dag_id: UUID,
        node_updates: list[tuple[UUID, dict]],
        reactivate: bool,
    ) -> None:
        """F087: apply every retry mutation AND the reactivation atomically.

        @codex P2 on a616310. Doing these as separate commits leaves an
        invalid state visible to the ordinary tick loop no matter which order
        they run in, which is why the previous reorder did not fix it:

        - resets first, reactivation last → the detached delivery sweep can
          select a still-terminal DAG on the OLD generation (so the fence
          cannot reject it) and announce an outcome built from half-reset
          nodes.
        - reactivation first, resets after → a tick loads a *running* DAG
          whose nodes are all still terminal, `_check_dag_completion` marks it
          failed again immediately, and the retry then strands a pending node
          inside a terminal DAG.

        There is no safe ordering because the invalid state is the split
        itself. One transaction removes the window rather than moving it: no
        observer ever sees a DAG whose status and node set disagree.
        """
        async with self._db.session() as session:
            scoped = select(ExecutionDAG.id).where(
                ExecutionDAG.agent_id == self._agent_id
            )
            for node_id, values in node_updates:
                await session.execute(
                    update(DAGNode)
                    .where(DAGNode.id == node_id)
                    .where(DAGNode.dag_id.in_(scoped))
                    .values(**values)
                )
            if reactivate:
                await session.execute(
                    update(ExecutionDAG)
                    .where(ExecutionDAG.id == dag_id)
                    .where(ExecutionDAG.agent_id == self._agent_id)
                    .values(
                        status="running",
                        started_at=datetime.now(UTC),
                        delivered_at=None,
                        delivery_attempts=0,
                        delivery_error=None,
                        delivery_summary=None,
                        delivery_generation=ExecutionDAG.delivery_generation + 1,
                    )
                )
            await session.commit()

    async def reactivate_for_retry(self, dag_id: UUID) -> None:
        """F087: put a terminal DAG back to 'running' AND clear its delivery
        bookkeeping, in one transaction.

        @codex P2 on b3c78c3: ``retry_node`` left ``delivered_at`` set when it
        reactivated a DAG, so the sweep's ``delivered_at IS NULL`` predicate
        excluded the retry's outcome forever — and the stale attempt count,
        error and cached summary all described the PREVIOUS outcome.

        @codex P2 on 9ca8fcd: doing that reset in its own commit after the
        status update reintroduced the same two-phase-write hazard this PR
        fixed for token accounting. A crash in between left a *running* DAG
        carrying a stale ``delivered_at``, which is worse than either endpoint:
        it finishes normally after restart and is then silently never
        announced. Status and delivery state are one logical transition, so
        they commit together.

        Mirrors ``update_dag_status(dag_id, "running")`` for the status half,
        including the ``started_at`` refresh, so reactivation semantics are
        unchanged.
        """
        async with self._db.session() as session:
            await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .values(
                    status="running",
                    started_at=datetime.now(UTC),
                    delivered_at=None,
                    delivery_attempts=0,
                    delivery_error=None,
                    delivery_summary=None,
                    # Invalidate any delivery still in flight for the previous
                    # outcome (@codex P1 on e94cd42).
                    delivery_generation=ExecutionDAG.delivery_generation + 1,
                )
            )
            await session.commit()

    async def bump_delivery_attempt(
        self, dag_id: UUID, generation: int, error: str
    ) -> int:
        """F087: record a failed delivery attempt. Returns the new count.

        Fenced like the other delivery writes (@codex P1 on fa988e7) so a
        failure belonging to the previous outcome cannot burn an attempt — or
        park a stale error — on a DAG that was reactivated mid-delivery.
        Returns 0 when the write did not apply.
        """
        async with self._db.session() as session:
            result = await session.execute(
                update(ExecutionDAG)
                .where(ExecutionDAG.id == dag_id)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .where(ExecutionDAG.delivered_at.is_(None))
                .where(ExecutionDAG.status.in_(_TERMINAL_DAG_STATUSES))
                .where(ExecutionDAG.delivery_generation == generation)
                .values(
                    delivery_attempts=ExecutionDAG.delivery_attempts + 1,
                    delivery_error=error[:2000],
                )
                .returning(ExecutionDAG.delivery_attempts)
            )
            await session.commit()
            row = result.scalar_one_or_none()
            return int(row) if row is not None else 0
