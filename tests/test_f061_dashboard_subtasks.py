"""F061 PR-3: tests for ``get_subtask_dashboard_data``.

Inserts ~20 synthetic subtask rows across outcomes and asserts the
aggregations produce the expected card shapes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from nous.api.dashboard_queries import get_subtask_dashboard_data
from nous.storage.models import Subtask


async def _seed_subtask(
    session,
    *,
    agent_id: str,
    final_outcome: str,
    status: str | None = None,
    completed_at: datetime | None = None,
    attempts: int = 1,
    tokens_in: int = 100,
    tokens_out: int = 50,
    tool_calls_made: int = 1,
    task: str = "default task",
    dag_node_id: uuid.UUID | None = None,
):
    """Insert one synthetic subtask row directly via SQL (bypass manager)."""
    if status is None:
        status = "completed" if final_outcome in {
            "completed", "incomplete_blocked",
        } else "failed"
    if completed_at is None:
        completed_at = datetime.now(UTC)
    await session.execute(
        insert(Subtask).values(
            id=uuid.uuid4(),
            agent_id=agent_id,
            task=task,
            priority=100,
            status=status,
            timeout_seconds=120,
            final_outcome=final_outcome,
            attempts=attempts,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tool_calls_made=tool_calls_made,
            completed_at=completed_at,
            dag_node_id=dag_node_id,
        )
    )


class TestSubtaskDashboardData:
    @pytest.mark.asyncio
    async def test_empty_window_returns_zeroed_card(self, session):
        agent_id = f"empty-{uuid.uuid4().hex[:8]}"
        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert data["window_hours"] == 24
        assert data["totals"]["total_terminal"] == 0
        assert data["totals"]["empty_rate"] == 0.0
        assert data["totals"]["retry_rate"] == 0.0
        assert data["top_failing_tasks"] == []
        assert data["recent_outcomes"] == []

    @pytest.mark.asyncio
    async def test_outcome_counts_aggregate_correctly(self, session):
        agent_id = f"counts-{uuid.uuid4().hex[:8]}"
        # 3 completed, 2 incomplete_no_terminal, 1 validation_failed
        for _ in range(3):
            await _seed_subtask(session, agent_id=agent_id, final_outcome="completed")
        for _ in range(2):
            await _seed_subtask(
                session, agent_id=agent_id,
                final_outcome="incomplete_no_terminal",
            )
        await _seed_subtask(
            session, agent_id=agent_id, final_outcome="validation_failed",
        )

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert data["totals"]["total_terminal"] == 6
        assert data["totals"]["by_outcome"]["completed"] == 3
        assert data["totals"]["by_outcome"]["incomplete_no_terminal"] == 2
        assert data["totals"]["by_outcome"]["validation_failed"] == 1

    @pytest.mark.asyncio
    async def test_empty_rate_calculation(self, session):
        agent_id = f"empty-rate-{uuid.uuid4().hex[:8]}"
        # 6 completed + 4 in {incomplete_no_terminal, validation_failed}
        for _ in range(6):
            await _seed_subtask(session, agent_id=agent_id, final_outcome="completed")
        for _ in range(2):
            await _seed_subtask(
                session, agent_id=agent_id,
                final_outcome="incomplete_no_terminal",
            )
        for _ in range(2):
            await _seed_subtask(
                session, agent_id=agent_id,
                final_outcome="validation_failed",
            )

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        # 4 / 10 = 0.4
        assert data["totals"]["empty_rate"] == pytest.approx(0.4, abs=0.001)

    @pytest.mark.asyncio
    async def test_retry_rate_calculation(self, session):
        agent_id = f"retry-{uuid.uuid4().hex[:8]}"
        # 3 with attempts=1, 2 with attempts=2 → retry_rate = 2/5
        for _ in range(3):
            await _seed_subtask(session, agent_id=agent_id, final_outcome="completed", attempts=1)
        for _ in range(2):
            await _seed_subtask(session, agent_id=agent_id, final_outcome="completed", attempts=2)

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert data["totals"]["retry_rate"] == pytest.approx(0.4, abs=0.001)

    @pytest.mark.asyncio
    async def test_tokens_by_outcome(self, session):
        agent_id = f"tokens-{uuid.uuid4().hex[:8]}"
        await _seed_subtask(
            session, agent_id=agent_id, final_outcome="completed",
            tokens_in=1000, tokens_out=200, tool_calls_made=5,
        )
        await _seed_subtask(
            session, agent_id=agent_id, final_outcome="completed",
            tokens_in=500, tokens_out=100, tool_calls_made=3,
        )

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert "completed" in data["tokens_by_outcome"]
        # Mean total tokens: ((1000+200) + (500+100)) / 2 = 900
        assert data["tokens_by_outcome"]["completed"]["mean_total_tokens"] == 900
        assert data["tokens_by_outcome"]["completed"]["mean_tool_calls"] == 4.0
        assert data["tokens_by_outcome"]["completed"]["n"] == 2

    @pytest.mark.asyncio
    async def test_top_failing_tasks_groups_by_task_prefix(self, session):
        agent_id = f"top-fail-{uuid.uuid4().hex[:8]}"
        # 3 fails of "research postgres", 1 fail of "find issue"
        for _ in range(3):
            await _seed_subtask(
                session, agent_id=agent_id,
                task="research postgres versions",
                final_outcome="incomplete_no_terminal",
            )
        await _seed_subtask(
            session, agent_id=agent_id,
            task="find issue in code",
            final_outcome="errored",
        )

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        top = data["top_failing_tasks"]
        assert len(top) == 2
        # Most failures first
        assert top[0]["task_prefix"] == "research postgres versions"
        assert top[0]["failures"] == 3
        assert top[0]["failure_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_dag_correlation_no_attached_returns_empty(self, session):
        """Negative case: no DAG-attached subtasks → empty correlation card."""
        agent_id = f"dag-corr-empty-{uuid.uuid4().hex[:8]}"
        for _ in range(3):
            await _seed_subtask(session, agent_id=agent_id, final_outcome="completed")

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert data["dag_correlation"] == {}

    @pytest.mark.asyncio
    async def test_dag_correlation_groups_attached_outcomes(self, session):
        """Positive case: subtasks with dag_node_id show up in dag_correlation
        grouped by their outcome.

        Closes the F061 PR-3 review P2-4 gap (architecture+code reviewers
        flagged the FK-attached path as untested).
        """
        from nous.storage.models import DAGNode, ExecutionDAG

        agent_id = f"dag-corr-{uuid.uuid4().hex[:8]}"

        # Create a minimal DAG + 2 nodes for the FK target.
        dag = ExecutionDAG(agent_id=agent_id, name="test-dag")
        session.add(dag)
        await session.flush()
        node_a = DAGNode(dag_id=dag.id, name="node-a", node_type="subtask", wave=0)
        node_b = DAGNode(dag_id=dag.id, name="node-b", node_type="subtask", wave=0)
        session.add_all([node_a, node_b])
        await session.flush()

        # Subtask attached to node_a — completed
        await _seed_subtask(
            session, agent_id=agent_id,
            final_outcome="completed", dag_node_id=node_a.id,
        )
        # Subtask attached to node_b — incomplete_no_terminal (a real failure)
        await _seed_subtask(
            session, agent_id=agent_id,
            final_outcome="incomplete_no_terminal", dag_node_id=node_b.id,
        )
        # Non-attached subtask — should NOT show up in dag_correlation.
        await _seed_subtask(
            session, agent_id=agent_id, final_outcome="completed",
        )

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert data["dag_correlation"] == {
            "completed": 1,
            "incomplete_no_terminal": 1,
        }
        # Total terminal includes the non-attached row
        assert data["totals"]["total_terminal"] == 3

    @pytest.mark.asyncio
    async def test_window_excludes_old_rows(self, session):
        agent_id = f"window-{uuid.uuid4().hex[:8]}"
        old = datetime.now(UTC) - timedelta(hours=48)
        recent = datetime.now(UTC) - timedelta(hours=1)
        await _seed_subtask(
            session, agent_id=agent_id, final_outcome="completed",
            completed_at=old,
        )
        await _seed_subtask(
            session, agent_id=agent_id, final_outcome="completed",
            completed_at=recent,
        )

        # 24h window — only the recent one should count
        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert data["totals"]["total_terminal"] == 1

        # 168h window — both
        data = await get_subtask_dashboard_data(session, agent_id, hours=168)
        assert data["totals"]["total_terminal"] == 2

    @pytest.mark.asyncio
    async def test_recent_outcomes_includes_id_and_dag_node(self, session):
        agent_id = f"recent-{uuid.uuid4().hex[:8]}"
        await _seed_subtask(
            session, agent_id=agent_id, final_outcome="completed",
            task="alpha task" + " " * 100,  # exercise LEFT(80) truncation
        )

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert len(data["recent_outcomes"]) == 1
        row = data["recent_outcomes"][0]
        assert "id" in row
        assert isinstance(row["id"], str)
        assert row["final_outcome"] == "completed"
        assert row["completed_at"] is not None
        assert len(row["task"]) <= 80
        assert row["dag_node_id"] is None

    @pytest.mark.asyncio
    async def test_legacy_pre_flag_rows_bucketed_as_unknown(self, session):
        """final_outcome IS NULL on pre-flag rows → 'unknown' bucket."""
        agent_id = f"legacy-{uuid.uuid4().hex[:8]}"
        # final_outcome=None goes into the 'unknown' bucket
        await session.execute(
            insert(Subtask).values(
                id=uuid.uuid4(),
                agent_id=agent_id,
                task="legacy",
                priority=100,
                status="completed",
                timeout_seconds=120,
                final_outcome=None,
                completed_at=datetime.now(UTC),
            )
        )

        data = await get_subtask_dashboard_data(session, agent_id, hours=24)
        assert data["totals"]["by_outcome"].get("unknown") == 1
