"""F038: DAG Store — CRUD operations for execution DAGs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from nous.dag.schemas import DAGCreateRequest
from nous.storage.database import Database
from nous.storage.models import DAGEdge, DAGNode, ExecutionDAG

logger = logging.getLogger(__name__)

MAX_ACTIVE_DAGS = 5


class DAGStore:
    """CRUD operations for DAG orchestration."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self._agent_id = agent_id

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
            )
            session.add(dag)
            await session.flush()  # Get dag.id

            # Create nodes
            node_map: dict[str, DAGNode] = {}
            for spec in request.nodes:
                wave = waves.get(spec.name, 0)
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
                    timeout_seconds=spec.timeout_seconds,
                    completion_condition=spec.completion_condition,
                    completion_check=spec.completion_check,
                    completion_check_interval=spec.completion_check_interval,
                    max_check_attempts=spec.max_check_attempts,
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
