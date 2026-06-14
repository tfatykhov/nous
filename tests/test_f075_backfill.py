"""F075.1 backfill — pure-unit tests for the non-obvious bits.

The script is one-time operational tooling validated end-to-end against a
real LLM on the eval DB (date extraction, undated-fact stamping, idempotency,
lock acquire/release). These tests lock in the properties that are easy to
silently break and were codex-flagged: the F047-collision-safe lock key,
BudgetTracker call-count semantics, and the malformed-vs-terminal stamp
decision (a malformed model date must NOT be stamped terminal).
"""
from __future__ import annotations

import hashlib
from datetime import date

import scripts.backfill_temporal_facts as backfill
from scripts.backfill_temporal_facts import (
    BudgetTracker,
    _advisory_lock_key,
    _classify_event_date,
)


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


def test_budget_tracker_zero_does_no_calls():
    """--token-budget 0 must gate the very first call (cost guard)."""
    assert not BudgetTracker(0).ok()


async def _classify_with_llm_returning(monkeypatch, payload):
    """Run _classify_event_date with the structured LLM stubbed to `payload`."""
    async def _stub(*_a, **_kw):
        return payload

    monkeypatch.setattr(backfill, "call_background_llm_structured", _stub)
    return await _classify_event_date(
        client=object(), model="m", row={"content": "x"}, chunk_context=None
    )


async def test_classify_valid_date_is_terminal(monkeypatch):
    """A clean YYYY-MM-DD records the date and is terminal (stamp)."""
    result = await _classify_with_llm_returning(monkeypatch, {"event_date": "2024-03-10"})
    assert result == (date(2024, 3, 10), True)


async def test_classify_explicit_null_is_terminal(monkeypatch):
    """An explicit null = 'no date': stamp terminal so it isn't re-processed."""
    result = await _classify_with_llm_returning(monkeypatch, {"event_date": None})
    assert result == (None, True)


async def test_classify_malformed_date_is_not_stamped(monkeypatch):
    """A non-null date the validator drops (e.g. 2024-3-10) must stay eligible."""
    result = await _classify_with_llm_returning(monkeypatch, {"event_date": "2024-3-10"})
    assert result == (None, False)  # should_stamp False -> retry next run


async def test_classify_no_result_is_not_stamped(monkeypatch):
    """A transient LLM failure (no structured result) stays eligible."""
    result = await _classify_with_llm_returning(monkeypatch, None)
    assert result == (None, False)


async def test_classify_injects_year_anchor_from_learned_at(monkeypatch):
    """When the row carries learned_at, the prompt anchors the YEAR to it so a
    relative date doesn't resolve to a prior year (the 365-day-chain bug)."""
    from datetime import datetime

    captured: dict = {}

    async def _stub(*_a, **kw):
        captured.update(kw)
        return {"event_date": None}

    monkeypatch.setattr(backfill, "call_background_llm_structured", _stub)
    await _classify_event_date(
        client=object(),
        model="m",
        row={"content": "deployed v2 last Tuesday", "learned_at": datetime(2026, 5, 25)},
        chunk_context=None,
    )
    assert "2026-05-25" in captured["user_message"]
    assert "never assume a prior year" in captured["user_message"]


async def test_classify_no_anchor_without_learned_at(monkeypatch):
    """Backward compat: a row lacking learned_at gets no anchor line."""
    captured: dict = {}

    async def _stub(*_a, **kw):
        captured.update(kw)
        return {"event_date": None}

    monkeypatch.setattr(backfill, "call_background_llm_structured", _stub)
    await _classify_event_date(
        client=object(), model="m", row={"content": "x"}, chunk_context=None
    )
    assert "recorded on" not in captured["user_message"]


def test_classify_system_excludes_bibliographic_and_month_granularity():
    """Regression: the two measured failure modes (bibliographic publication
    dates, month-only false precision) must stay excluded in the system prompt."""
    sys = backfill._CLASSIFY_SYSTEM.lower()
    assert "arxiv" in sys and "publication" in sys
    assert "specific day" in sys and "1st of the month" in sys
