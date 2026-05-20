"""F062 Commit A: tests for payload_schema column + SubtaskResult dataclass.

Covers:
- ORM round-trip for the two new columns (payload_schema JSONB,
  payload_schema_valid BOOLEAN).
- SubtaskResult dataclass shape + to_dict() serialization.
- SubtaskOutcome Literal alias values match heart.subtasks.final_outcome's
  canonical seven (the alignment is enforced by hand here; if the F061 enum
  ever drifts we want this test to break loudly).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.models import SubtaskOutcome, SubtaskResult
from nous.storage.models import Subtask


# Canonical strings F061 writes to heart.subtasks.final_outcome
# (sql/migrations/041_subtask_hardening.sql:38). SubtaskOutcome MUST match.
_F061_OUTCOMES = {
    "completed",
    "incomplete_blocked",
    "incomplete_no_terminal",
    "validation_failed",
    "timed_out",
    "errored",
    "cancelled",
}


class TestSubtaskOutcomeAlias:
    def test_subtask_outcome_literal_covers_all_f061_strings(self) -> None:
        # __args__ on Literal[...] returns the tuple of literal values.
        assert set(SubtaskOutcome.__args__) == _F061_OUTCOMES  # type: ignore[attr-defined]


class TestSubtaskResultDataclass:
    def test_minimal_construction(self) -> None:
        r = SubtaskResult(
            task_id="00000000-0000-0000-0000-000000000001",
            status="completed",
            payload={"key": "value"},
            raw_text="some prose summary",
            confidence=0.82,
            elapsed_seconds=12.3456,
        )
        assert r.validator_reason is None
        # to_dict rounds elapsed_seconds to 3 decimals.
        assert r.to_dict()["elapsed_seconds"] == 12.346

    def test_payload_is_any_not_dict_only(self) -> None:
        """payload must accept arrays, scalars, and None — not just dict."""
        for payload in (
            [1, 2, 3],
            "a string",
            42,
            3.14,
            True,
            None,
            {"x": 1},
        ):
            r = SubtaskResult(
                task_id="t",
                status="completed",
                payload=payload,
                raw_text="",
                confidence=None,
                elapsed_seconds=0.1,
            )
            assert r.to_dict()["payload"] == payload

    def test_validation_failed_status_with_reason(self) -> None:
        r = SubtaskResult(
            task_id="t",
            status="validation_failed",
            payload={},
            raw_text="<raw report text>",
            confidence=None,
            elapsed_seconds=2.0,
            validator_reason="payload schema mismatch: 'name' is a required property",
        )
        d = r.to_dict()
        assert d["status"] == "validation_failed"
        assert d["validator_reason"].startswith("payload schema mismatch")
        assert d["payload"] == {}


class TestSubtaskPayloadSchemaColumns:
    """ORM round-trip for the F062 columns."""

    async def test_defaults_are_null(self, session: AsyncSession) -> None:
        s = Subtask(
            agent_id="test-agent",
            task="t",
            priority=100,
            timeout_seconds=60,
        )
        session.add(s)
        await session.flush()
        assert s.payload_schema is None
        assert s.payload_schema_valid is None

    async def test_roundtrip_schema_and_valid_flag(
        self, session: AsyncSession
    ) -> None:
        schema = {
            "type": "object",
            "required": ["name", "score"],
            "properties": {
                "name": {"type": "string"},
                "score": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
        s = Subtask(
            agent_id="test-agent",
            task="t",
            priority=100,
            timeout_seconds=60,
            payload_schema=schema,
            payload_schema_valid=True,
        )
        session.add(s)
        await session.flush()
        sid = s.id

        loaded = (
            await session.execute(select(Subtask).where(Subtask.id == sid))
        ).scalar_one()
        assert loaded.payload_schema == schema
        assert loaded.payload_schema_valid is True

    async def test_valid_can_be_false(self, session: AsyncSession) -> None:
        s = Subtask(
            agent_id="test-agent",
            task="t",
            priority=100,
            timeout_seconds=60,
            payload_schema={"type": "object"},
            payload_schema_valid=False,
        )
        session.add(s)
        await session.flush()
        assert s.payload_schema_valid is False
