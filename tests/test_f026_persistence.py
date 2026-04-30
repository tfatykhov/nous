"""F026 decision persistence — unit tests for the runner emit hook.

Verifies that ActionGate verdicts and ClaimVerifier outcomes get
fire-and-forwarded to Brain.emit_event so a retrospective accuracy eval
can run against real prod data later.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.api.runner import AgentRunner
from nous.config import Settings


def _runner_with_mocks(persistence_enabled: bool = True) -> tuple[AgentRunner, MagicMock]:
    """Build an AgentRunner with mocked dependencies for unit testing.

    Returns (runner, brain_mock) so tests can assert on emit_event calls.
    """
    settings = Settings(f026_persistence_enabled=persistence_enabled)
    brain = MagicMock()
    brain.emit_event = AsyncMock()
    cognitive = MagicMock()
    heart = MagicMock()
    runner = AgentRunner(cognitive, brain, heart, settings)
    return runner, brain


@pytest.mark.asyncio
async def test_log_f026_decision_emits_event_when_enabled():
    """Persistence flag ON: emit_event must be invoked via create_task."""
    runner, brain = _runner_with_mocks(persistence_enabled=True)
    runner._log_f026_decision(
        "f026_action_gate",
        {"tool_name": "write_file", "approved": True, "reason": "no-duplicates"},
        session_id="sess-1",
    )
    # Allow the create_task fire to schedule + run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    brain.emit_event.assert_awaited_once()
    args, kwargs = brain.emit_event.call_args
    # First positional is event_type, second is data dict.
    assert args[0] == "f026_action_gate"
    assert args[1]["tool_name"] == "write_file"
    assert args[1]["approved"] is True
    assert kwargs["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_log_f026_decision_skipped_when_disabled():
    """Persistence flag OFF: must be a no-op, no emit_event call."""
    runner, brain = _runner_with_mocks(persistence_enabled=False)
    runner._log_f026_decision(
        "f026_action_gate",
        {"tool_name": "write_file", "approved": False, "reason": "duplicate"},
        session_id="sess-1",
    )
    await asyncio.sleep(0)
    brain.emit_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_log_f026_decision_swallows_errors():
    """Persistence is best-effort — emit_event raising must not propagate."""
    runner, brain = _runner_with_mocks(persistence_enabled=True)
    brain.emit_event = AsyncMock(side_effect=RuntimeError("DB down"))
    # Must not raise.
    runner._log_f026_decision(
        "f026_action_gate", {"tool_name": "x", "approved": True, "reason": ""},
        session_id="sess-1",
    )
    # Run scheduled task; the inner exception is swallowed by create_task's
    # error handling — the outer call must remain clean.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_log_f026_claim_verification_event_shape():
    """Claim verification event carries violation details for retrospective eval."""
    runner, brain = _runner_with_mocks(persistence_enabled=True)
    runner._log_f026_decision(
        "f026_claim_verification",
        {
            "verified": False,
            "violation_count": 1,
            "violations": [
                {
                    "claimed_text": "I sent the email",
                    "expected_tool": "send_email",
                    "found_in_turn": False,
                    "found_in_ledger": False,
                }
            ],
            "tool_names_this_turn": [],
            "mode": "enforce",
        },
        session_id="sess-2",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    args, _ = brain.emit_event.call_args
    assert args[0] == "f026_claim_verification"
    data = args[1]
    assert data["verified"] is False
    assert data["violation_count"] == 1
    assert data["violations"][0]["expected_tool"] == "send_email"
