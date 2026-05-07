"""F049 Mechanism B tests — subtask session teardown via try/finally.

Verifies that `_execute_subtask` always calls `runner.end_conversation` on
every exit path (success, failure, cancellation, outer timeout) and that
the cleanup itself is bounded, shielded, and fails loudly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.config import Settings
from nous.handlers.subtask_worker import SubtaskWorkerPool
from nous.storage.models import Subtask


def _make_subtask() -> MagicMock:
    subtask = MagicMock(spec=Subtask)
    subtask.id = uuid.uuid4()
    subtask.task = "F049 cleanup test"
    subtask.parent_session_id = None
    subtask.timeout_seconds = 60
    subtask.frame_type = None
    subtask.model = None
    subtask.notify = False
    return subtask


def _make_settings(*, subtask_cleanup_timeout_seconds: int = 30, **overrides) -> Settings:
    base = {
        "subtask_workers": 1,
        "subtask_poll_interval": 0.1,
        "subtask_default_timeout": 120,
        "subtask_max_concurrent": 3,
        "telegram_bot_token": None,
        "telegram_chat_id": None,
    }
    base.update(overrides)
    settings = Settings(**base)
    # subtask_cleanup_timeout_seconds has a validation_alias, so it can't be
    # passed as a constructor kwarg by field name — set it post-construction.
    settings.subtask_cleanup_timeout_seconds = subtask_cleanup_timeout_seconds
    return settings


def _make_heart() -> MagicMock:
    heart = MagicMock()
    heart.subtasks = AsyncMock()
    heart.subtasks.complete = AsyncMock()
    heart.subtasks.fail = AsyncMock()
    return heart


def _expected_session_id(subtask: MagicMock) -> str:
    return f"subtask-{subtask.id.hex[:8]}"


async def test_execute_subtask_success_ends_conversation():
    """Happy path: end_conversation called exactly once with the right session_id."""
    runner = AsyncMock()
    runner.run_turn = AsyncMock(return_value=("ok", MagicMock(), {}))
    runner.end_conversation = AsyncMock()

    heart = _make_heart()
    settings = _make_settings()
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    await pool._execute_subtask(subtask)

    runner.end_conversation.assert_awaited_once()
    call = runner.end_conversation.await_args
    assert call.args[0] == _expected_session_id(subtask)
    assert call.kwargs.get("agent_id") == settings.agent_id
    heart.subtasks.complete.assert_awaited_once()


async def test_execute_subtask_failure_ends_conversation(caplog):
    """run_turn raises → subtask marked failed AND end_conversation still called once."""
    runner = AsyncMock()
    runner.run_turn = AsyncMock(side_effect=ValueError("boom"))
    runner.end_conversation = AsyncMock()

    heart = _make_heart()
    settings = _make_settings()
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    with caplog.at_level(logging.ERROR, logger="nous.handlers.subtask_worker"):
        await pool._execute_subtask(subtask)

    runner.end_conversation.assert_awaited_once()
    heart.subtasks.fail.assert_awaited_once()
    fail_args = heart.subtasks.fail.await_args
    assert "ValueError" in fail_args.args[1]
    assert any("failed" in rec.message.lower() for rec in caplog.records)


async def test_execute_subtask_cancellation_ends_conversation():
    """Outer task cancelled mid-run_turn → end_conversation awaited before CancelledError propagates."""
    started = asyncio.Event()
    ended = asyncio.Event()

    async def blocking_run_turn(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()  # block forever
        finally:
            pass

    async def recording_end(*_args, **_kwargs):
        ended.set()

    runner = AsyncMock()
    runner.run_turn = blocking_run_turn
    runner.end_conversation = recording_end

    heart = _make_heart()
    settings = _make_settings()
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    task = asyncio.create_task(pool._execute_subtask(subtask))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # asyncio.shield lets end_conversation continue as a detached task after
    # the awaiter is cancelled. Yield once so the shielded coroutine actually
    # runs before we assert its completion.
    await asyncio.sleep(0)
    assert ended.is_set(), "end_conversation must run in the finally before cancel propagates"


async def test_execute_subtask_outer_timeout_ends_conversation():
    """asyncio.wait_for on the outer call times out → inner finally still runs end_conversation."""
    started = asyncio.Event()
    ended = asyncio.Event()

    async def blocking_run_turn(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def recording_end(*_args, **_kwargs):
        ended.set()

    runner = AsyncMock()
    runner.run_turn = blocking_run_turn
    runner.end_conversation = recording_end

    heart = _make_heart()
    settings = _make_settings()
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pool._execute_subtask(subtask), timeout=0.05)

    # Give the shielded cleanup a moment to complete after the cancel storm.
    await asyncio.wait_for(ended.wait(), timeout=1.0)
    assert ended.is_set()


async def test_process_subtask_timeout_persists_attempts_at_least_one():
    """F061 PR-3 Codex review: worker outer-timeout path must pass
    attempts >= 1 to heart.subtasks.fail() so the dashboard retry-rate
    metric isn't skewed by timeout rows persisting attempts=0.

    Round 4: ``attempts`` is now read from HardenedRunState. With the
    legacy path (no state set), attempts falls back to 1.
    """
    runner = AsyncMock()

    async def blocking_run_turn(**_kwargs):
        await asyncio.Event().wait()

    runner.run_turn = blocking_run_turn
    runner.end_conversation = AsyncMock()

    heart = _make_heart()
    settings = _make_settings()
    # Force flag-off → legacy path → no state populated → falls back to 1.
    settings.subtask_hardening_enabled = False
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    subtask.timeout_seconds = 0.05

    await pool._process_subtask(subtask)

    heart.subtasks.fail.assert_awaited_once()
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs.get("final_outcome") == "timed_out"
    assert kwargs.get("attempts", 0) >= 1, (
        "timeout path must persist attempts>=1; one execution attempt happened "
        "before the timeout. attempts=0 would skew dashboard retry-rate metrics."
    )


async def test_process_subtask_timeout_reads_real_attempt_count_from_state():
    """F061 round 4 P1-A: hardened-path timeout reads HardenedRunState
    for the actual in-progress attempt count rather than hardcoding 1.

    Setup: pool runs hardened path; state is mutated in-place by
    execute_hardened. We patch execute_hardened to set state.attempts=2
    (simulating "currently on attempt 2") then block forever.
    """
    from unittest.mock import patch

    from nous.handlers.subtask_executor import HardenedRunState

    runner = AsyncMock()
    runner.end_conversation = AsyncMock()
    heart = _make_heart()
    settings = _make_settings(subtask_hardening_enabled=True)
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    subtask.timeout_seconds = 0.05
    subtask.output_format = None
    subtask.success_criteria = None
    subtask.frame_type = None

    # Patch execute_hardened to mutate state to attempt=2 then block.
    async def fake_execute_hardened(_subtask, _session_id, *, state, **_kw):
        state.attempts = 2
        state.tokens_in = 1234
        state.tokens_out = 567
        state.tool_calls_made = 4
        await asyncio.Event().wait()  # block until cancelled

    with patch(
        "nous.handlers.subtask_executor.execute_hardened",
        side_effect=fake_execute_hardened,
    ):
        await pool._process_subtask(subtask)

    heart.subtasks.fail.assert_awaited_once()
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs.get("final_outcome") == "timed_out"
    assert kwargs.get("attempts") == 2, (
        "round 4 P1-A: timeout must read actual attempt count from "
        "HardenedRunState, not hardcode 1"
    )
    assert kwargs.get("tokens_in") == 1234
    assert kwargs.get("tokens_out") == 567
    assert kwargs.get("tool_calls_made") == 4


async def test_process_subtask_timeout_emits_outcome_event():
    """F061 round 4 P1-D: outer timeout handler emits a subtask_outcome
    event so event-driven telemetry consumers don't silently miss
    timeouts (execute_hardened skipped emission on cancel).
    """
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import patch

    from nous.handlers.subtask_executor import (
        SUBTASK_OUTCOME_EVENT_TYPE,
    )

    runner = AsyncMock()
    runner.end_conversation = AsyncMock()
    heart = _make_heart()
    settings = _make_settings(subtask_hardening_enabled=True)
    bus = MagicMock()
    bus.emit = _AsyncMock()
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings, bus=bus)

    subtask = _make_subtask()
    subtask.timeout_seconds = 0.05
    subtask.output_format = None
    subtask.success_criteria = None
    subtask.frame_type = None

    async def fake_execute_hardened(_subtask, _session_id, *, state, **_kw):
        state.attempts = 1
        state.tokens_in = 100
        state.tokens_out = 50
        await asyncio.Event().wait()

    with patch(
        "nous.handlers.subtask_executor.execute_hardened",
        side_effect=fake_execute_hardened,
    ):
        await pool._process_subtask(subtask)

    # bus.emit must have been called with a subtask_outcome event for
    # the timed_out outcome.
    timed_out_emits = [
        c for c in bus.emit.await_args_list
        if c.args and c.args[0].type == SUBTASK_OUTCOME_EVENT_TYPE
        and c.args[0].data.get("final_outcome") == "timed_out"
    ]
    assert len(timed_out_emits) == 1, (
        f"expected exactly one subtask_outcome 'timed_out' emit; "
        f"got {len(timed_out_emits)}"
    )
    event_data = timed_out_emits[0].args[0].data
    assert event_data["attempts"] == 1
    assert event_data["tokens_in"] == 100


async def test_cleanup_timeout_logs_error(caplog):
    """end_conversation hangs past cleanup timeout → TimeoutError branch hits with ERROR log."""
    runner = AsyncMock()
    runner.run_turn = AsyncMock(return_value=("ok", MagicMock(), {}))

    async def slow_end(*_args, **_kwargs):
        await asyncio.sleep(3)  # > cleanup_timeout

    runner.end_conversation = slow_end

    heart = _make_heart()
    settings = _make_settings(subtask_cleanup_timeout_seconds=1)
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    with caplog.at_level(logging.ERROR, logger="nous.handlers.subtask_worker"):
        await pool._execute_subtask(subtask)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("timed out" in r.message.lower() for r in error_records), (
        f"expected ERROR log about cleanup timeout, got: {[r.message for r in caplog.records]}"
    )


async def test_cleanup_exception_logs_exception_and_preserves_original(caplog):
    """run_turn raises RuntimeError (→ fail path), end_conversation raises ConnectionError.
    Both the heart.subtasks.fail call for the original error and the cleanup logger.exception
    must fire; neither swallows the other."""
    runner = AsyncMock()
    runner.run_turn = AsyncMock(side_effect=RuntimeError("primary failure"))
    runner.end_conversation = AsyncMock(side_effect=ConnectionError("cleanup boom"))

    heart = _make_heart()
    settings = _make_settings()
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    with caplog.at_level(logging.ERROR, logger="nous.handlers.subtask_worker"):
        await pool._execute_subtask(subtask)

    # Inner except ran: fail was called for the original RuntimeError.
    heart.subtasks.fail.assert_awaited_once()
    fail_args = heart.subtasks.fail.await_args
    assert "RuntimeError" in fail_args.args[1]

    # Cleanup logger.exception fired for the ConnectionError.
    cleanup_errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "cleanup failed" in r.message.lower()
    ]
    assert cleanup_errors, (
        f"expected ERROR 'cleanup failed' record, got: {[r.message for r in caplog.records]}"
    )
    assert cleanup_errors[0].exc_info is not None, "logger.exception must attach exc_info"


async def test_cleanup_cancelled_reraises(caplog):
    """A CancelledError raised inside end_conversation (second-cancel scenario) → WARNING log + reraise."""
    runner = AsyncMock()
    runner.run_turn = AsyncMock(return_value=("ok", MagicMock(), {}))
    runner.end_conversation = AsyncMock(side_effect=asyncio.CancelledError())

    heart = _make_heart()
    settings = _make_settings()
    pool = SubtaskWorkerPool(runner=runner, heart=heart, settings=settings)

    subtask = _make_subtask()
    with caplog.at_level(logging.WARNING, logger="nous.handlers.subtask_worker"):
        with pytest.raises(asyncio.CancelledError):
            await pool._execute_subtask(subtask)

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "cancelled" in r.message.lower()
    ]
    assert warnings, (
        f"expected WARNING 'cancelled' record, got: {[r.message for r in caplog.records]}"
    )
