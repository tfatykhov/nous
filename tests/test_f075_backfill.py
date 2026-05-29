"""F075.1 backfill — pure-unit tests for the non-obvious bits.

The script is one-time operational tooling validated end-to-end against a
real LLM on the eval DB (date extraction, undated-fact stamping, idempotency,
lock acquire/release). These tests lock in the two properties that are easy
to silently break and were codex-flagged during the F075 spec rounds:
the F047-collision-safe lock key, and BudgetTracker call-count semantics.
"""
from __future__ import annotations

import hashlib

from scripts.backfill_temporal_facts import BudgetTracker, _advisory_lock_key


def test_advisory_lock_key_namespaced_off_f047():
    """F075 lock key must differ from F047's bare-agent_id key (no collision)."""
    agent = "nous-default"
    f047_bare = int.from_bytes(
        hashlib.sha256(agent.encode()).digest()[:8], "big", signed=True
    )
    f075 = _advisory_lock_key(agent)
    assert f075 != f047_bare, "F075 lock must not collide with F047's bare-agent key"
    # stable + agent-specific
    assert _advisory_lock_key(agent) == f075
    assert _advisory_lock_key("other") != f075


def test_budget_tracker_call_count_semantics():
    """consume() decrements by 1; ok() gates the next call (not token-count)."""
    b = BudgetTracker(2)
    assert b.ok()
    b.consume()
    assert b.ok()
    b.consume()
    assert not b.ok()  # exhausted after exactly 2 calls
