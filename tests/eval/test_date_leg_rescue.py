"""Unit tests for nous_eval.date_leg_rescue (F075 L3 Task 7)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.eval

from nous_eval.date_leg_rescue import rrf_fuse


def test_rrf_fuse_is_position_based():
    # gold at rank 3 in vanilla, rank 1 in the leg -> fusion lifts it
    vanilla = ["a", "b", "gold", "c"]
    leg = ["gold", "x"]
    fused = rrf_fuse(vanilla, leg)
    assert fused.index("gold") < 2  # lifted into the head
