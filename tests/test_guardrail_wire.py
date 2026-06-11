"""BR-4/6: the CEL guardrail engine is wired into deliberation.finalize as an
advisory check — it evaluates and surfaces guardrails but never rejects the
decision. These tests use a mocked Brain so no DB is required."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.brain.schemas import GuardrailResult
from nous.cognitive.deliberation import DeliberationEngine

_DESC = "Adopt cursor-based pagination for the public decisions API endpoint"


def _brain_with(guardrail_result: GuardrailResult) -> MagicMock:
    brain = MagicMock()
    detail = MagicMock(stakes="high", category="architecture", quality_score=0.6, reasons=[])
    brain.get = AsyncMock(return_value=detail)
    brain.update = AsyncMock()
    brain.check = AsyncMock(return_value=guardrail_result)
    return brain


@pytest.mark.asyncio
async def test_guardrail_block_is_advisory_not_rejecting():
    """A blocking guardrail is evaluated and logged but the decision is still
    finalized (advisory wire — not enforcement)."""
    did = str(uuid.uuid4())
    brain = _brain_with(GuardrailResult(allowed=False, blocked_by=["no-high-stakes-low-confidence"], warnings=[]))
    delib = DeliberationEngine(brain, MagicMock(guardrail_check_enabled=True))

    result = await delib.finalize(did, _DESC, confidence=0.3)

    assert result == did  # finalized despite the block
    brain.check.assert_awaited_once()
    brain.update.assert_awaited_once()


@pytest.mark.asyncio
async def test_guardrail_check_skipped_when_disabled():
    did = str(uuid.uuid4())
    brain = _brain_with(GuardrailResult(allowed=True, blocked_by=[], warnings=[]))
    delib = DeliberationEngine(brain, MagicMock(guardrail_check_enabled=False))

    result = await delib.finalize(did, _DESC, confidence=0.3)

    assert result == did
    brain.check.assert_not_awaited()


@pytest.mark.asyncio
async def test_guardrail_engine_error_does_not_break_finalize():
    """If the engine raises (e.g. a malformed CEL expression), finalize still
    succeeds — the check is best-effort."""
    did = str(uuid.uuid4())
    brain = _brain_with(GuardrailResult(allowed=True, blocked_by=[], warnings=[]))
    brain.check = AsyncMock(side_effect=RuntimeError("CEL boom"))
    delib = DeliberationEngine(brain, MagicMock(guardrail_check_enabled=True))

    result = await delib.finalize(did, _DESC, confidence=0.3)

    assert result == did
