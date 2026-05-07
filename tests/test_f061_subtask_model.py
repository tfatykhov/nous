"""F061 PR-1: tests for the new Subtask ORM columns."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.storage.models import Subtask


class TestSubtaskF061Columns:
    """Round-trip the 9 new mapped columns."""

    async def test_minimal_subtask_has_zero_counters_and_null_optionals(
        self, session: AsyncSession
    ) -> None:
        """Default insert: counters = 0, optional fields NULL."""
        s = Subtask(
            agent_id="test-agent",
            task="minimal task",
            priority=100,
            timeout_seconds=120,
        )
        session.add(s)
        await session.flush()

        # Defaults
        assert s.attempts == 0
        assert s.tokens_in == 0
        assert s.tokens_out == 0
        assert s.tool_calls_made == 0

        # NULL optionals
        assert s.report_jsonb is None
        assert s.final_outcome is None
        assert s.output_format is None
        assert s.success_criteria is None
        assert s.dag_node_id is None

    async def test_full_round_trip(self, session: AsyncSession) -> None:
        """All new fields persist and reload correctly."""
        report = {
            "summary": "Found 3 candidates matching the criteria.",
            "findings": ["candidate A", "candidate B", "candidate C"],
            "next_actions": ["review A first"],
            "confidence": 0.82,
            "evidence_refs": ["fact-uuid-1"],
            "incomplete": False,
            "blocked_reason": "",
        }
        s = Subtask(
            agent_id="test-agent",
            task="full task",
            priority=100,
            timeout_seconds=120,
            report_jsonb=report,
            final_outcome="completed",
            attempts=2,
            tokens_in=1234,
            tokens_out=567,
            tool_calls_made=8,
            output_format="JSON-style summary with findings list.",
            success_criteria="At least 2 candidates returned.",
        )
        session.add(s)
        await session.flush()
        sid = s.id

        # Re-fetch
        result = await session.execute(select(Subtask).where(Subtask.id == sid))
        loaded = result.scalar_one()
        assert loaded.report_jsonb == report
        assert loaded.final_outcome == "completed"
        assert loaded.attempts == 2
        assert loaded.tokens_in == 1234
        assert loaded.tokens_out == 567
        assert loaded.tool_calls_made == 8
        assert loaded.output_format == "JSON-style summary with findings list."
        assert loaded.success_criteria == "At least 2 candidates returned."

    async def test_dag_node_id_accepts_none(self, session: AsyncSession) -> None:
        """Common path: dag_node_id=None round-trips cleanly."""
        s = Subtask(
            agent_id="test-agent",
            task="t",
            priority=100,
            timeout_seconds=60,
            dag_node_id=None,
        )
        session.add(s)
        await session.flush()
        assert s.dag_node_id is None

    async def test_dag_node_id_fk_rejects_orphan(self, session: AsyncSession) -> None:
        """FK constraint rejects a UUID with no matching nous_system.dag_nodes row.

        The migration test (test_f061_migration_041.py::test_fk_constraint_present)
        verifies the FK is wired with ON DELETE SET NULL. This test verifies
        the ORM-side referential integrity actually holds.
        """
        s = Subtask(
            agent_id="test-agent",
            task="t",
            priority=100,
            timeout_seconds=60,
            dag_node_id=uuid.uuid4(),  # random UUID — no matching dag_nodes row
        )
        session.add(s)
        with pytest.raises(IntegrityError):
            await session.flush()
        # Roll back the failed transaction so the session fixture's outer
        # rollback doesn't trip on a deassociated state. Avoids SAWarning.
        await session.rollback()
