"""Unit tests for nous_eval.date_leg_rescue (F075 L3 Task 7)."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.eval

from nous_eval.date_leg_rescue import _generate_temporal_query, rrf_fuse


def test_rrf_fuse_is_position_based():
    # gold at rank 3 in vanilla, rank 1 in the leg -> fusion lifts it
    vanilla = ["a", "b", "gold", "c"]
    leg = ["gold", "x"]
    fused = rrf_fuse(vanilla, leg)
    assert fused.index("gold") < 2  # lifted into the head


class _CaptureClient:
    """Fake anthropic client that records the payload and returns a text block."""
    def __init__(self, raises: bool = False):
        self._raises = raises
        self.payload = None

    async def call(self, payload):
        self.payload = payload
        if self._raises:
            raise RuntimeError("boom")
        return SimpleNamespace(content=[{"type": "text", "text": "What happened in June 2026?"}])


@pytest.mark.asyncio
async def test_generate_temporal_query_payload_has_system_key():
    # Regression: the real anthropic client requires payload["system"]; omitting it
    # raised KeyError and silently degraded every query to the content-leaking fallback.
    client = _CaptureClient()
    q = await _generate_temporal_query(client, "m", "some fact about calibration", datetime.date(2026, 6, 1))
    assert "system" in client.payload and client.payload["system"], "payload must carry a non-empty system"
    assert q == "What happened in June 2026?"


@pytest.mark.asyncio
async def test_fallback_query_does_not_leak_fact_content():
    # Regression: on gen failure the fallback MUST NOT echo the fact content, or vanilla
    # finds the gold trivially and rescue headroom collapses.
    client = _CaptureClient(raises=True)
    content = "SECRETKEYWORD calibration Brier 0.192"
    q = await _generate_temporal_query(client, "m", content, datetime.date(2026, 6, 1))
    assert "SECRETKEYWORD" not in q and "calibration" not in q
    assert "2026" in q
