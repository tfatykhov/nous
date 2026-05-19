"""Tests for F064.5 — scheduled task Episode reuse (v1 partial).

v1 ships Episode reuse only — each fire still starts a fresh LLM context
because `runner.end_conversation` pops the in-memory Conversation. True
thread continuity (sending only "continuation guidance") is deferred to
F064.5-v2 with explicit state serialization.

Acceptance criterion (plan §8.4): a schedule with continuation_turns=5
reuses the same Episode across up to 5 consecutive fires (via stable
session_id), then resets on the 6th. Every fire (including 2-5) sends the
full task prompt because v1 doesn't implement LLM thread continuity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.handlers.task_scheduler import TaskScheduler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _continuation_on(continuation_turns: int = 3) -> Settings:
    return Settings(
        schedule_continuation_enabled=True,
        schedule_max_continuation_turns=50,
    )


def _continuation_off() -> Settings:
    return Settings(schedule_continuation_enabled=False)


def _make_schedule(
    *,
    continuation_turns: int = 0,
    continuation_session_id: str | None = None,
    continuation_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id="test",
        task="do thing",
        schedule_type="recurring",
        interval_seconds=60,
        cron_expr=None,
        last_fired_at=None,
        next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
        fire_count=0,
        max_fires=None,
        notify=False,
        timeout_seconds=120,
        created_by_session=None,
        model=None,
        frame_type=None,
        continuation_turns=continuation_turns,
        continuation_session_id=continuation_session_id,
        continuation_prompt=None,
        continuation_count=continuation_count,
        active=True,
    )


def _mock_heart_with_schedule(schedule, has_active_subtask: bool = False) -> MagicMock:
    """Build a Heart mock that returns the given schedule from get_due()."""
    heart = MagicMock()
    heart.schedules = MagicMock()
    heart.schedules.get_due = AsyncMock(return_value=[schedule])
    heart.schedules.deactivate = AsyncMock()
    heart.schedules.advance = AsyncMock()
    heart.schedules.set_continuation_session = AsyncMock()
    heart.schedules.bump_continuation_count = AsyncMock()
    heart.schedules.reset_continuation = AsyncMock()
    heart.subtasks = MagicMock()
    heart.subtasks.create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    # Fail-open path: _has_active_subtask_for_schedule swallows errors,
    # so a bare MagicMock for heart.db works.
    heart.db = MagicMock()
    return heart


# ---------------------------------------------------------------------------
# Continuation cycle behavior
# ---------------------------------------------------------------------------


class TestContinuationCycle:
    @pytest.mark.asyncio
    async def test_continuation_disabled_emits_no_session_id(self):
        """continuation_turns=0 → no session_id in metadata, today's behavior."""
        schedule = _make_schedule(continuation_turns=0)
        heart = _mock_heart_with_schedule(schedule)
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        call = heart.subtasks.create.call_args
        meta = call.kwargs["metadata"]
        assert "session_id" not in meta
        heart.schedules.set_continuation_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_continuation_first_fire_mints_fresh_session(self):
        """First fire of a continuation cycle → set_continuation_session called
        with a new session_id; metadata carries it."""
        schedule = _make_schedule(continuation_turns=3)
        heart = _mock_heart_with_schedule(schedule)
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        call = heart.subtasks.create.call_args
        meta = call.kwargs["metadata"]
        assert meta["session_id"].startswith("schedule-")
        heart.schedules.set_continuation_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_continuation_reuses_existing_session_mid_cycle(self):
        """When continuation_session_id is set and count < turns,
        the existing session_id is reused (no fresh mint)."""
        existing_session = "schedule-aaaaaaaa-1"
        schedule = _make_schedule(
            continuation_turns=3,
            continuation_session_id=existing_session,
            continuation_count=1,
        )
        heart = _mock_heart_with_schedule(schedule)
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        call = heart.subtasks.create.call_args
        assert call.kwargs["metadata"]["session_id"] == existing_session
        heart.schedules.set_continuation_session.assert_not_called()
        heart.schedules.bump_continuation_count.assert_called_once()

    @pytest.mark.asyncio
    async def test_continuation_resets_at_cap(self):
        """When the next dispatch would hit the cap, reset_continuation is
        called so the following fire starts a fresh cycle."""
        schedule = _make_schedule(
            continuation_turns=3,
            continuation_session_id="schedule-aaaaaaaa-1",
            continuation_count=2,  # one more dispatch → count=3 == cap
        )
        heart = _mock_heart_with_schedule(schedule)
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        # After this dispatch the count would hit the cap → reset.
        heart.schedules.reset_continuation.assert_called_once()

    @pytest.mark.asyncio
    async def test_settings_cap_overrides_per_schedule_turns(self):
        """Per-schedule turns=100, settings cap=2 → effective cap is 2.
        At count=1, next dispatch hits effective_cap=2 → reset."""
        schedule = _make_schedule(
            continuation_turns=100,
            continuation_session_id="schedule-aaaaaaaa-1",
            continuation_count=1,
        )
        heart = _mock_heart_with_schedule(schedule)
        settings = Settings(
            schedule_continuation_enabled=True,
            schedule_max_continuation_turns=2,
        )
        scheduler = TaskScheduler(heart, settings)
        await scheduler._fire_due_tasks()
        heart.schedules.reset_continuation.assert_called_once()


# ---------------------------------------------------------------------------
# Running-subtask debounce (architecture P1-A)
# ---------------------------------------------------------------------------


class TestEnqueueFailureLeavesCycleClean:
    @pytest.mark.asyncio
    async def test_failed_enqueue_does_not_persist_continuation_state(self):
        """@codex P2 on e119510: when subtasks.create raises (queue full,
        DB hiccup), we must NOT have persisted set_continuation_session or
        bump_continuation_count. Otherwise the schedule points at a
        phantom session that was never actually dispatched."""
        schedule = _make_schedule(continuation_turns=3)
        heart = _mock_heart_with_schedule(schedule)
        # Make create() raise the queue-full ValueError that task_scheduler
        # explicitly catches in its outer except.
        heart.subtasks.create = AsyncMock(side_effect=ValueError("queue full"))
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        heart.schedules.set_continuation_session.assert_not_called()
        heart.schedules.bump_continuation_count.assert_not_called()
        heart.schedules.reset_continuation.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_enqueue_does_not_bump_existing_cycle(self):
        """Mid-cycle: existing session_id, count=1. Enqueue fails. Count
        must STAY at 1 (no phantom bump)."""
        schedule = _make_schedule(
            continuation_turns=3,
            continuation_session_id="schedule-aaaaaaaa-1",
            continuation_count=1,
        )
        heart = _mock_heart_with_schedule(schedule)
        heart.subtasks.create = AsyncMock(side_effect=ValueError("queue full"))
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        heart.schedules.bump_continuation_count.assert_not_called()


class TestLifecycleAdvanceSurvivesContinuationFailure:
    @pytest.mark.asyncio
    async def test_continuation_state_failure_still_advances_schedule(self):
        """@codex P2 on a167217: if continuation-state writes (set_session
        / bump_count / reset) raise, schedule.advance() MUST still run.
        Otherwise the schedule's next_fire_at stays stale and the
        scheduler re-fires forever."""
        schedule = _make_schedule(continuation_turns=3)
        heart = _mock_heart_with_schedule(schedule)
        # Make set_continuation_session raise — this is the fresh-cycle path.
        heart.schedules.set_continuation_session = AsyncMock(
            side_effect=RuntimeError("simulated DB hiccup")
        )
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        # advance MUST have been called despite the continuation-state failure
        heart.schedules.advance.assert_called_once()

    @pytest.mark.asyncio
    async def test_continuation_state_failure_on_reuse_still_advances(self):
        schedule = _make_schedule(
            continuation_turns=3,
            continuation_session_id="schedule-aaaaaaaa-1",
            continuation_count=1,
        )
        heart = _mock_heart_with_schedule(schedule)
        # Mid-cycle: bump_continuation_count raises.
        heart.schedules.bump_continuation_count = AsyncMock(
            side_effect=RuntimeError("simulated DB hiccup")
        )
        scheduler = TaskScheduler(heart, _continuation_on())
        await scheduler._fire_due_tasks()
        heart.schedules.advance.assert_called_once()


class TestRunningSubtaskDebounce:
    @pytest.mark.asyncio
    async def test_debounce_skips_fire_when_active_subtask_exists(self):
        """When _has_active_subtask_for_schedule returns True, the schedule
        is NOT fired (no subtask created) and continuation state isn't
        touched. Architecture P1-A safeguard."""
        schedule = _make_schedule(continuation_turns=3)
        heart = _mock_heart_with_schedule(schedule)
        scheduler = TaskScheduler(heart, _continuation_on())
        # Stub the debounce check to return True.
        scheduler._has_active_subtask_for_schedule = AsyncMock(return_value=True)
        await scheduler._fire_due_tasks()
        heart.subtasks.create.assert_not_called()
        heart.schedules.advance.assert_not_called()
        heart.schedules.set_continuation_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_debounce_check_fails_open_on_error(self):
        """If _has_active_subtask_for_schedule raises (mock, DB hiccup),
        the fire proceeds — fail-OPEN. The schedule's next_fire_at gating
        is the primary correctness barrier; this check is a safety belt."""
        schedule = _make_schedule()
        heart = _mock_heart_with_schedule(schedule)
        scheduler = TaskScheduler(heart, _continuation_off())
        # heart.db is a bare MagicMock — the inline query raises; the
        # method should swallow and return False (allow fire).
        await scheduler._fire_due_tasks()
        heart.subtasks.create.assert_called_once()
