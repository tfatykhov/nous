"""F061 PR-3: tests for the subtask_outcome event emitter.

Per silent-failure spec review P1.5: ``emit_outcome_event`` MUST wrap its
body in try/except so a DB / event-bus failure is logged at ERROR severity,
not silently dropped. Without this, fire-and-forget telemetry recreates
the exact silent-failure pattern F061 is built to fix.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.handlers.subtask_executor import (
    SUBTASK_OUTCOME_EVENT_TYPE,
    emit_outcome_event,
)
from nous.heart.subtask_report import SubtaskReport
from nous.heart.subtask_validator import ValidationResult


def _settings(persistence_enabled: bool = True):
    return SimpleNamespace(
        agent_id="test-agent",
        subtask_outcome_persistence_enabled=persistence_enabled,
    )


def _subtask(*, id_=None, agent_id="test-agent", dag_node_id=None):
    return SimpleNamespace(
        id=id_ or uuid.uuid4(),
        agent_id=agent_id,
        frame_type="research",
        dag_node_id=dag_node_id,
    )


@pytest.mark.asyncio
async def test_emits_subtask_outcome_event_on_completed():
    bus = MagicMock()
    bus.emit = AsyncMock()
    subtask = _subtask()
    report = SubtaskReport(summary="x" * 60, confidence=0.9)
    result = ValidationResult.passed(report)

    await emit_outcome_event(
        bus, subtask, result, payload_dict := report.model_dump(),
        settings=_settings(),
        duration_ms=1234, attempts=1,
        tokens_in=500, tokens_out=200, tool_calls_made=3,
    )

    bus.emit.assert_awaited_once()
    event = bus.emit.await_args.args[0]
    assert event.type == SUBTASK_OUTCOME_EVENT_TYPE
    assert event.agent_id == "test-agent"
    assert event.session_id.startswith("subtask-")
    d = event.data
    assert d["final_outcome"] == "completed"
    assert d["ok"] is True
    assert d["validator_reason"] is None
    assert d["attempts"] == 1
    assert d["tokens_in"] == 500
    assert d["tokens_out"] == 200
    assert d["tool_calls_made"] == 3
    assert d["duration_ms"] == 1234
    assert d["dag_node_id"] is None
    assert d["frame_type"] == "research"
    # subtask_id is the str-form UUID
    assert d["subtask_id"] == str(subtask.id)
    # payload_dict variable is exercised; ensure no kwargs collision (sanity)
    assert isinstance(payload_dict, dict)


@pytest.mark.asyncio
async def test_emits_validation_failed_with_reason():
    bus = MagicMock()
    bus.emit = AsyncMock()
    subtask = _subtask()
    result = ValidationResult.failed(
        "validation_failed", "summary_too_short: len=12 (min 50)",
    )

    await emit_outcome_event(
        bus, subtask, result, None,
        settings=_settings(), duration_ms=500, attempts=2,
        tokens_in=100, tokens_out=50, tool_calls_made=1,
    )

    bus.emit.assert_awaited_once()
    d = bus.emit.await_args.args[0].data
    assert d["final_outcome"] == "validation_failed"
    assert d["ok"] is False
    assert "summary_too_short" in d["validator_reason"]
    assert d["attempts"] == 2


@pytest.mark.asyncio
async def test_dag_node_id_is_stringified():
    bus = MagicMock()
    bus.emit = AsyncMock()
    dag_uuid = uuid.uuid4()
    subtask = _subtask(dag_node_id=dag_uuid)
    result = ValidationResult.passed(
        SubtaskReport(summary="x" * 60, confidence=0.5),
    )

    await emit_outcome_event(
        bus, subtask, result, None,
        settings=_settings(), duration_ms=10, attempts=1,
        tokens_in=0, tokens_out=0, tool_calls_made=0,
    )

    d = bus.emit.await_args.args[0].data
    assert d["dag_node_id"] == str(dag_uuid)


@pytest.mark.asyncio
async def test_skipped_when_persistence_disabled():
    bus = MagicMock()
    bus.emit = AsyncMock()
    subtask = _subtask()
    result = ValidationResult.passed(SubtaskReport(summary="x" * 60, confidence=1.0))

    await emit_outcome_event(
        bus, subtask, result, None,
        settings=_settings(persistence_enabled=False),
    )
    bus.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_skipped_when_bus_is_none():
    """No bus → no-op without raising."""
    subtask = _subtask()
    result = ValidationResult.passed(SubtaskReport(summary="x" * 60, confidence=0.5))
    # Must not raise even with bus=None
    await emit_outcome_event(
        None, subtask, result, None, settings=_settings(),
    )


@pytest.mark.asyncio
async def test_bus_emit_failure_is_logged_not_propagated(caplog):
    """Silent-failure spec review P1.5: DB write errors must be loud, not silent.

    The fire-and-forget contract means callers won't await the emit. So if
    the emit raises and there's no inner try/except, the exception is lost
    and operators have no signal that telemetry vanished. This test
    asserts the inner try/except + logger.exception path.
    """
    bus = MagicMock()
    bus.emit = AsyncMock(side_effect=RuntimeError("DB unavailable"))
    subtask = _subtask()
    result = ValidationResult.passed(SubtaskReport(summary="x" * 60, confidence=0.5))

    with caplog.at_level(logging.ERROR, logger="nous.handlers.subtask_executor"):
        # Must NOT raise — the executor catches and logs.
        await emit_outcome_event(
            bus, subtask, result, None, settings=_settings(),
        )

    # Verify a loud ERROR-level log entry exists.
    matching = [
        r for r in caplog.records
        if "Failed to emit" in r.message and r.levelno >= logging.ERROR
    ]
    assert matching, (
        "Expected ERROR-level 'Failed to emit ...' log; got: "
        f"{[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# All 7 outcome variants emit correctly (per spec mech-10 + plan §3.1)
# ---------------------------------------------------------------------------


class TestAllOutcomeVariantsEmit:
    """Each value of the 7-state outcome enum must emit a valid event."""

    @pytest.mark.asyncio
    async def test_incomplete_blocked_emits_with_blocked_reason_in_validator_reason(self):
        bus = MagicMock()
        bus.emit = AsyncMock()
        subtask = _subtask()
        report = SubtaskReport(
            summary="x", confidence=0.0,
            incomplete=True, blocked_reason="permission denied",
        )
        result = ValidationResult.incomplete("permission denied", report)

        await emit_outcome_event(
            bus, subtask, result, report.model_dump(),
            settings=_settings(), duration_ms=100, attempts=1,
            tokens_in=10, tokens_out=5, tool_calls_made=0,
        )
        d = bus.emit.await_args.args[0].data
        assert d["final_outcome"] == "incomplete_blocked"
        assert d["ok"] is False
        # validator_reason carries the blocked_reason on incomplete_blocked
        assert d["validator_reason"] == "permission denied"

    @pytest.mark.asyncio
    async def test_incomplete_no_terminal_emits(self):
        bus = MagicMock()
        bus.emit = AsyncMock()
        result = ValidationResult.failed(
            "incomplete_no_terminal",
            "Subtask exited without calling submit_final_report.",
        )
        await emit_outcome_event(
            bus, _subtask(), result, None,
            settings=_settings(), duration_ms=200, attempts=2,
            tokens_in=300, tokens_out=100, tool_calls_made=5,
        )
        d = bus.emit.await_args.args[0].data
        assert d["final_outcome"] == "incomplete_no_terminal"
        assert d["ok"] is False
        assert "submit_final_report" in d["validator_reason"]

    @pytest.mark.asyncio
    async def test_timed_out_emits(self):
        bus = MagicMock()
        bus.emit = AsyncMock()
        result = ValidationResult.failed("timed_out", "Subtask timed out after 600s")
        await emit_outcome_event(
            bus, _subtask(), result, None,
            settings=_settings(), duration_ms=600_000, attempts=1,
            tokens_in=200, tokens_out=80, tool_calls_made=2,
        )
        d = bus.emit.await_args.args[0].data
        assert d["final_outcome"] == "timed_out"
        assert d["ok"] is False

    @pytest.mark.asyncio
    async def test_errored_emits_with_exception_class_in_reason(self):
        bus = MagicMock()
        bus.emit = AsyncMock()
        result = ValidationResult.failed("errored", "RuntimeError: API 503")
        await emit_outcome_event(
            bus, _subtask(), result, None,
            settings=_settings(), duration_ms=50, attempts=1,
            tokens_in=0, tokens_out=0, tool_calls_made=0,
        )
        d = bus.emit.await_args.args[0].data
        assert d["final_outcome"] == "errored"
        assert "RuntimeError" in d["validator_reason"]

    @pytest.mark.asyncio
    async def test_cancelled_emits(self):
        """F061 PR-3 silent-failure review P1.1: cancellation must still
        produce an event so the dashboard never silently misses a subtask.
        """
        bus = MagicMock()
        bus.emit = AsyncMock()
        result = ValidationResult.failed(
            "cancelled", "Subtask cancelled (worker shutdown).",
        )
        await emit_outcome_event(
            bus, _subtask(), result, None,
            settings=_settings(), duration_ms=300, attempts=1,
            tokens_in=100, tokens_out=20, tool_calls_made=1,
        )
        d = bus.emit.await_args.args[0].data
        assert d["final_outcome"] == "cancelled"
        assert d["ok"] is False
        assert "cancelled" in d["validator_reason"].lower()
