"""F038: DAG Store — CRUD operations for execution DAGs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from nous.config import Settings
from nous.dag.schemas import DAGCreateRequest
from nous.storage.database import Database
from nous.storage.models import DAGEdge, DAGNode, ExecutionDAG, Subtask

logger = logging.getLogger(__name__)

MAX_ACTIVE_DAGS = 5


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
                if spec.stall_timeout_seconds is None or spec.stall_timeout_seconds == 0:
                    resolved_stall = spec.stall_timeout_seconds  # preserve None vs 0
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
                node = DAGNode(
                    dag_id=dag.id,
                    name=spec.name,
                    description=spec.description,
                    node_type=spec.type.value,
                    wave=wave,
                    status="ready" if wave == 0 else "pending",
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

    async def get_recent_dags(self, limit: int = 20) -> list[ExecutionDAG]:
        """Get recent DAGs for dashboard display."""
        async with self._db.session() as session:
            result = await session.execute(
                select(ExecutionDAG)
                .where(ExecutionDAG.agent_id == self._agent_id)
                .options(
                    selectinload(ExecutionDAG.nodes),
                    selectinload(ExecutionDAG.edges),
                )
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

    async def count_running_subtasks_by_frame_type(self) -> dict[str, int]:
        """F064.2: grouped count of agent-scoped IN-FLIGHT subtasks by frame_type.

        Returns a dict {frame_type: count} including the "_default" bucket for
        subtasks whose frame_type is NULL. Used by orchestrator dispatch gating
        to enforce per-frame caps without keeping in-memory state across ticks.

        Counts both `status='pending'` AND `status='running'` (@codex P1 on
        c3a4fed): SubtaskManager.create inserts in 'pending', and the worker
        only transitions to 'running' on dequeue. Between dispatch and pickup
        a launched subtask is invisible to a 'running'-only count, so a
        subsequent tick would over-dispatch through the cap.

        Single grouped SELECT — mirrors heart/subtasks.py:293 count_by_status
        pattern (verified equivalent at codex review time).
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Subtask.frame_type, func.count())
                .where(Subtask.agent_id == self._agent_id)
                .where(Subtask.status.in_(["pending", "running"]))
                .group_by(Subtask.frame_type)
            )
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
