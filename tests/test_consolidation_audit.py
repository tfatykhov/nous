"""F035.6 — Consolidation Audit Diff unit tests.

Exercises the ``ConsolidationAuditor`` against an in-memory fake DB (no Postgres)
plus the sleep-handler kill-switch wiring. Covers the spec's acceptance criteria:

- A2 envelope: open writes a ``running`` row; close writes ``completed`` + totals.
- A2 invariant: ``actions_persisted <= totals`` (equality only on full success).
- A6 retention: prune deletes; disabled when days <= 0.
- A7 kill-switch: disabled settings => handler builds no auditor (no writes).
- A8 FK degradation: open failure => cycle_id=None, actions still written.
- A9 ordering: close drains pending action batches before the completed write.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from nous.handlers.consolidation_audit import ConsolidationAuditor, make_trace_id, preview


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, db: "_FakeDB") -> None:
        self._db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if self._db.fail_on_open and "INSERT INTO nous_system.consolidation_cycles" in sql:
            raise RuntimeError("forced cycle-open failure")
        if "INSERT INTO nous_system.consolidation_cycles" in sql:
            self._db.cycles.append(dict(params))
            return _FakeResult(1)
        if "UPDATE nous_system.consolidation_cycles" in sql:
            self._db.closes.append(dict(params))
            return _FakeResult(1)
        if "INSERT INTO nous_system.consolidation_actions" in sql:
            rows = params if isinstance(params, list) else [params]
            if self._db.slow_insert:
                await asyncio.sleep(0.02)
            # simulate a lossy batch when requested
            persisted = rows if not self._db.drop_inserts else rows[:-1]
            self._db.actions.extend(persisted)
            self._db.order.append("actions")
            return _FakeResult(len(persisted))
        if "DELETE FROM nous_system.consolidation_actions" in sql:
            return _FakeResult(self._db.delete_count)
        return _FakeResult(0)

    async def commit(self):
        return None


class _FakeDB:
    def __init__(self) -> None:
        self.cycles: list[dict] = []
        self.closes: list[dict] = []
        self.actions: list[dict] = []
        self.order: list[str] = []
        self.fail_on_open = False
        self.slow_insert = False
        self.drop_inserts = False
        self.delete_count = 0

    def session(self):
        return _FakeSession(self)


def _auditor(db, **kw) -> ConsolidationAuditor:
    return ConsolidationAuditor(db, "test-agent", **kw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_make_trace_id_is_12_chars():
    assert len(make_trace_id()) == 12


def test_preview_truncates():
    assert preview("x" * 500) == "x" * 200
    assert preview(None) == ""


# ---------------------------------------------------------------------------
# Envelope (A2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_writes_running_cycle():
    db = _FakeDB()
    aud = _auditor(db)
    await aud.open()
    assert len(db.cycles) == 1
    assert db.cycles[0]["aid"] == "test-agent"
    assert aud.cycle_id is not None
    assert len(aud.trace_id) == 12


@pytest.mark.asyncio
async def test_record_flush_close_persists_actions():
    db = _FakeDB()
    aud = _auditor(db)
    await aud.open()
    aud.record("reflect", "learn", target_ids=[uuid.uuid4()], after={"x": 1}, rationale="r")
    aud.record("stale_scan", "deactivate", target_ids=[uuid.uuid4()])
    await aud.close("completed", ["reflect", "stale_scan"], {"facts_created": 1, "stale_deactivated": 1})

    assert aud.actions_recorded == 2
    assert len(db.actions) == 2
    assert aud.actions_persisted == 2
    # close wrote the terminal envelope
    assert len(db.closes) == 1
    assert db.closes[0]["st"] == "completed"
    assert db.closes[0]["phases"] == ["reflect", "stale_scan"]


@pytest.mark.asyncio
async def test_totals_invariant_persisted_le_recorded():
    # A2: a lossy batch leaves actions_persisted < actions_recorded.
    db = _FakeDB()
    db.drop_inserts = True  # last row of each batch silently dropped
    aud = _auditor(db)
    await aud.open()
    aud.record("reflect", "learn")
    aud.record("reflect", "learn")
    await aud.close("completed", ["reflect"], {"facts_created": 2})
    assert aud.actions_recorded == 2
    assert aud.actions_persisted <= aud.actions_recorded
    assert aud.actions_persisted == 1  # one of two dropped


# ---------------------------------------------------------------------------
# Orphan degradation (A8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_failure_degrades_to_orphan_rows():
    db = _FakeDB()
    db.fail_on_open = True
    aud = _auditor(db)
    await aud.open()
    assert aud.cycle_id is None          # degraded
    assert len(aud.trace_id) == 12       # but trace_id survives
    aud.record("reflect", "learn")
    await aud.close("completed", ["reflect"], {"facts_created": 1})
    # action still written, carrying trace_id, cycle_id NULL
    assert len(db.actions) == 1
    assert db.actions[0]["cycle_id"] is None
    assert db.actions[0]["trace_id"] == aud.trace_id
    # no envelope close attempted (cycle never opened)
    assert len(db.closes) == 0


# ---------------------------------------------------------------------------
# Drain ordering (A9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_drains_pending_batches_before_close_write():
    db = _FakeDB()
    db.slow_insert = True  # the batch insert sleeps before recording
    aud = _auditor(db)
    await aud.open()
    aud.record("reflect", "learn")
    await aud.flush()  # spawns a slow background batch task
    # the action has NOT landed yet (still sleeping)
    assert db.order == []
    await aud.close("completed", ["reflect"], {"facts_created": 1})
    # close must have drained the batch (actions) BEFORE writing the close
    assert db.order == ["actions"]
    assert len(db.actions) == 1
    assert len(db.closes) == 1


# ---------------------------------------------------------------------------
# Retention (A6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_disabled_when_days_non_positive():
    db = _FakeDB()
    db.delete_count = 99
    aud = _auditor(db)
    assert await aud.prune_old_actions(0) == 0
    assert await aud.prune_old_actions(-5) == 0


@pytest.mark.asyncio
async def test_prune_deletes_old_rows():
    db = _FakeDB()
    db.delete_count = 7
    aud = _auditor(db)
    assert await aud.prune_old_actions(30) == 7


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_awaits_inline_over_cap():
    db = _FakeDB()
    aud = _auditor(db, max_inflight=1)
    await aud.open()
    # First flush spawns a task; with cap=1 the second flush awaits inline.
    aud.record("reflect", "learn")
    await aud.flush()
    aud.record("reflect", "learn")
    await aud.flush()
    await aud.close("completed", ["reflect"], {"facts_created": 2})
    assert len(db.actions) == 2


# ---------------------------------------------------------------------------
# Kill-switch at the handler level (A7)
# ---------------------------------------------------------------------------


def _make_handler(db, audit_enabled: bool):
    from unittest.mock import AsyncMock, MagicMock

    from nous.events import EventBus
    from nous.handlers.sleep_handler import SleepHandler

    brain = AsyncMock()
    heart = AsyncMock()
    heart.db = db
    settings = MagicMock()
    settings.agent_id = "test-agent"
    settings.consolidation_audit_enabled = audit_enabled
    settings.consolidation_audit_retention_days = 30
    settings.consolidation_audit_max_inflight = 32
    bus = MagicMock(spec=EventBus)
    bus.on = MagicMock()
    bus.emit = AsyncMock()
    handler = SleepHandler(brain, heart, settings, bus, AsyncMock())
    return handler


def _sleep_event():
    from nous.events import Event

    return Event(type="sleep_started", agent_id="test-agent", data={}, session_id="s1")


@pytest.mark.asyncio
async def test_killswitch_off_opens_no_cycle():
    db = _FakeDB()
    handler = _make_handler(db, audit_enabled=False)
    await handler._run_sleep(_sleep_event())
    assert db.cycles == []
    assert db.closes == []
    assert handler._auditor is None


@pytest.mark.asyncio
async def test_killswitch_on_opens_and_closes_cycle():
    db = _FakeDB()
    handler = _make_handler(db, audit_enabled=True)
    await handler._run_sleep(_sleep_event())
    assert len(db.cycles) == 1            # envelope opened
    assert len(db.closes) == 1            # envelope closed (completed)
    assert db.closes[0]["st"] == "completed"
    assert handler._auditor is None       # cleared in finally
