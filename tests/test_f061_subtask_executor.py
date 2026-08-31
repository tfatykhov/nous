"""F061 PR-2: tests for the shared execute_hardened helper.

These tests script ``runner.run_turn`` to return scripted ``(text, ctx, usage)``
tuples while a fake collector inside the runner mock simulates what
``submit_final_report`` calls would do. We're NOT testing the runner here —
that's covered by ``test_f061_runner_subtask_hooks.py``. The focus is the
executor's per-attempt loop, validator integration, retry policy, and
``_persist_outcome`` mapping.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.handlers.subtask_executor import execute_hardened


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subtask(**overrides):
    """Build a Subtask-shaped namespace usable by execute_hardened."""
    base = dict(
        id=uuid.uuid4(),
        task="research X",
        frame_type="research",
        model=None,
        output_format=None,
        success_criteria=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_settings(max_attempts: int = 2, force: bool = True):
    return SimpleNamespace(
        agent_id="agent-test",
        background_model="claude-haiku-4-5-20251001",
        subtask_max_attempts=max_attempts,
        subtask_report_min_summary_chars=50,
        subtask_tool_call_limit=20,
        subtask_force_tool_on_penultimate=force,
    )


def _make_heart_mock():
    h = MagicMock()
    h.subtasks = MagicMock()
    h.subtasks.complete = AsyncMock()
    h.subtasks.fail = AsyncMock()
    return h


def _scripted_runner(*, scripted_payloads, scripted_usages=None):
    """Build a runner mock whose run_turn:
      1. populates the collector (mimicking submit_final_report dispatch)
      2. returns the scripted (text, ctx, usage) tuple

    scripted_payloads: list of dicts (one per attempt) — each gets pushed
    into the collector via extra_tools["submit_final_report"][1]. Pass None
    to simulate "model didn't call the tool" (collector remains empty).
    scripted_usages: list of usage dicts to return; defaults all 100/50/1.
    """
    runner = MagicMock()
    call_idx = {"i": 0}

    if scripted_usages is None:
        scripted_usages = [
            {"input_tokens": 100, "output_tokens": 50, "tool_calls": 1}
            for _ in scripted_payloads
        ]

    async def _run_turn(**kwargs):
        i = call_idx["i"]
        call_idx["i"] += 1
        # Drive the collector — extra_tools is in kwargs
        extra = kwargs.get("extra_tools") or {}
        if "submit_final_report" in extra:
            _schema, executor = extra["submit_final_report"]
            payload = scripted_payloads[i] if i < len(scripted_payloads) else None
            if payload is not None:
                await executor(**payload)
        usage = (
            scripted_usages[i]
            if i < len(scripted_usages)
            else {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
        )
        return ("Report submitted." if (extra and i < len(scripted_payloads)
                and scripted_payloads[i] is not None) else "ran",
                MagicMock(),
                usage)

    runner.run_turn = AsyncMock(side_effect=_run_turn)
    return runner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_one_attempt_completes():
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "Found 3 candidates matching the search criteria. "
                   "Each was reviewed for fitness.",
        "confidence": 0.9,
        "findings": ["a", "b", "c"],
    }])
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask()

    final_text, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )

    assert result.ok is True
    assert result.outcome == "completed"
    assert "Found 3 candidates" in final_text
    # complete() called with final_outcome=completed and report_jsonb populated
    heart.subtasks.complete.assert_awaited_once()
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["final_outcome"] == "completed"
    assert kwargs["attempts"] == 1
    assert kwargs["tokens_in"] == 100
    assert kwargs["tokens_out"] == 50
    assert kwargs["tool_calls_made"] == 1
    assert kwargs["report_jsonb"]["confidence"] == 0.9


@pytest.mark.asyncio
async def test_empty_then_valid_retry_succeeds_in_two_attempts():
    """Attempt 1: collector empty (model didn't call tool). Attempt 2: valid."""
    runner = _scripted_runner(scripted_payloads=[
        None,  # no submit_final_report
        {
            "summary": "After investigation the answer is clearly option A "
                       "based on three concrete reasons.",
            "confidence": 0.8,
        },
    ])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask()

    final_text, result = await execute_hardened(
        subtask, "sess-2",
        runner=runner, heart=heart, settings=settings,
    )

    assert result.ok is True
    assert "option A" in final_text
    # Two run_turn calls (one per attempt)
    assert runner.run_turn.await_count == 2
    # Persisted with attempts=2 and accumulated tokens
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["final_outcome"] == "completed"
    assert kwargs["attempts"] == 2
    assert kwargs["tokens_in"] == 200  # 100 + 100
    assert kwargs["tokens_out"] == 100  # 50 + 50
    assert kwargs["tool_calls_made"] == 2  # 1 + 1


@pytest.mark.asyncio
async def test_two_empties_outcome_incomplete_no_terminal():
    """Both attempts empty → final_outcome=incomplete_no_terminal, status=failed."""
    runner = _scripted_runner(scripted_payloads=[None, None])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask()

    final_text, result = await execute_hardened(
        subtask, "sess-3",
        runner=runner, heart=heart, settings=settings,
    )

    assert result.ok is False
    assert result.outcome == "incomplete_no_terminal"
    # fail() called, not complete()
    heart.subtasks.complete.assert_not_called()
    heart.subtasks.fail.assert_awaited_once()
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["final_outcome"] == "incomplete_no_terminal"
    assert kwargs["attempts"] == 2


@pytest.mark.asyncio
async def test_row_metadata_caps_attempts_below_the_setting():
    """F092.2 (codex P1): agent-action subtasks set metadata max_attempts=1
    because a validation retry re-runs the WHOLE objective — one tap must
    never execute its side effects twice. A valid second payload exists,
    but the row cap means it is never requested."""
    runner = _scripted_runner(scripted_payloads=[None, {
        "status": "success",
        "summary": "This would only be reachable on a second attempt "
                   "which the row cap forbids.",
        "confidence": 0.8,
    }])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask(metadata_={"max_attempts": 1})

    final_text, result = await execute_hardened(
        subtask, "sess-cap",
        runner=runner, heart=heart, settings=settings,
    )

    assert result.ok is False
    assert result.outcome == "incomplete_no_terminal"
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["attempts"] == 1, "the row cap must stop the retry"
    # The cap can only LOWER attempts, never raise them past the setting.
    settings2 = _make_settings(max_attempts=2)
    subtask2 = _make_subtask(metadata_={"max_attempts": 5})
    runner2 = _scripted_runner(scripted_payloads=[None, None, None, None, None])
    _, result2 = await execute_hardened(
        subtask2, "sess-cap2",
        runner=runner2, heart=_make_heart_mock(), settings=settings2,
    )
    assert result2.outcome == "incomplete_no_terminal"


@pytest.mark.asyncio
async def test_placeholder_summary_then_valid_retries_to_success():
    runner = _scripted_runner(scripted_payloads=[
        {"summary": "I will research and report back when I find more.", "confidence": 0.5},
        {
            "summary": "PostgreSQL 17.2 is the current stable version "
                       "released in November 2024.",
            "confidence": 0.95,
        },
    ])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask()

    _final, result = await execute_hardened(
        subtask, "sess-4",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.ok is True
    assert heart.subtasks.complete.await_args.kwargs["attempts"] == 2


@pytest.mark.asyncio
async def test_incomplete_blocked_skips_retry():
    """incomplete=true → no retry; status=completed, final_outcome=incomplete_blocked."""
    runner = _scripted_runner(scripted_payloads=[
        {
            "summary": "blocked",
            "confidence": 0.0,
            "incomplete": True,
            "blocked_reason": "permission denied on /etc/shadow",
        },
    ])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask()

    _final, result = await execute_hardened(
        subtask, "sess-5",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "incomplete_blocked"
    # NO retry — exactly one run_turn call
    assert runner.run_turn.await_count == 1
    # Persisted via complete (status='completed') with final_outcome=incomplete_blocked
    heart.subtasks.complete.assert_awaited_once()
    heart.subtasks.fail.assert_not_called()
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["final_outcome"] == "incomplete_blocked"


@pytest.mark.asyncio
async def test_api_exception_first_attempt_persists_errored():
    """run_turn raises → caught per-attempt → final_outcome='errored'.

    Addresses silent-failure P1.3: without this catch, the AttributeError
    on a None last_result would mask the real exception.
    """
    runner = MagicMock()
    runner.run_turn = AsyncMock(side_effect=RuntimeError("API 503"))
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask()

    _final, result = await execute_hardened(
        subtask, "sess-6",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.ok is False
    assert result.outcome == "errored"
    assert "RuntimeError" in result.reason
    assert "API 503" in result.reason
    heart.subtasks.fail.assert_awaited_once()
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["final_outcome"] == "errored"
    assert kwargs["attempts"] == 1


@pytest.mark.asyncio
async def test_telemetry_callbacks_fire():
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "x" * 60, "confidence": 0.5,
    }])
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask()

    emit = AsyncMock()
    notify = AsyncMock()

    await execute_hardened(
        subtask, "sess-7",
        runner=runner, heart=heart, settings=settings,
        emit_event=emit, notify_telegram=notify,
    )
    emit.assert_awaited_once()
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_emit_failure_does_not_propagate():
    """If telemetry callback raises, executor does not propagate (loud log only)."""
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "x" * 60, "confidence": 0.5,
    }])
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask()

    emit = AsyncMock(side_effect=RuntimeError("bus down"))

    # Should NOT raise — defensive try/except inside execute_hardened
    final, result = await execute_hardened(
        subtask, "sess-8",
        runner=runner, heart=heart, settings=settings,
        emit_event=emit,
    )
    assert result.ok is True
    # Persistence still happened
    heart.subtasks.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_message_uses_configured_min_summary_chars():
    """F061 PR-3 Codex follow-up review: the retry prompt must reflect the
    operator-configured ``NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS`` rather
    than the hardcoded 50-char baseline. Otherwise raising the threshold
    to 100 would trap subtasks in a permanent retry loop because the model
    obeys the stale 50-char instruction and gets rejected every time.
    """
    from nous.handlers.subtask_executor import _build_retry_message

    # Default (50 chars)
    msg = _build_retry_message(
        task="t",
        prior_payload={"summary": "x", "confidence": 0.5},
        reason="summary_too_short: len=1 (min 50)",
        min_summary_chars=50,
    )
    assert "summary >= 50 chars" in msg

    # Operator-raised threshold
    msg_custom = _build_retry_message(
        task="t",
        prior_payload={"summary": "x", "confidence": 0.5},
        reason="summary_too_short: len=1 (min 100)",
        min_summary_chars=100,
    )
    assert "summary >= 100 chars" in msg_custom
    assert "summary >= 50 chars" not in msg_custom


@pytest.mark.asyncio
async def test_executor_passes_min_summary_to_retry_message():
    """End-to-end: when settings.subtask_report_min_summary_chars=100 and
    attempt 1 produces a too-short summary, the retry user_message must
    instruct the model on the 100-char threshold, not the 50-char default.
    """
    runner = _scripted_runner(scripted_payloads=[
        # Attempt 1: 60 chars — passes default 50 but fails custom 100
        {
            "summary": "x" * 60,
            "confidence": 0.5,
        },
        # Attempt 2: 110 chars — passes
        {
            "summary": "y" * 110,
            "confidence": 0.7,
        },
    ])
    heart = _make_heart_mock()
    settings = _make_settings()
    settings.subtask_report_min_summary_chars = 100  # operator override
    subtask = _make_subtask()

    final, result = await execute_hardened(
        subtask, "sess-thresh",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.ok is True

    # Check that on the second run_turn invocation, the user_message included
    # the 100-char threshold (i.e., retry message used the active min).
    second_call_kwargs = runner.run_turn.call_args_list[1].kwargs
    second_user_message = second_call_kwargs["user_message"]
    assert "summary >= 100 chars" in second_user_message


@pytest.mark.asyncio
async def test_cancellation_does_not_persist_or_emit():
    """F061 PR-3 Codex review P1: the finally block MUST NOT persist or
    emit a 'cancelled' outcome on CancelledError, because the function
    runs under asyncio.wait_for in both worker and inline paths — a normal
    timeout arrives as cancellation FIRST, before the outer caller catches
    TimeoutError and writes final_outcome='timed_out'. Persisting from
    inside the finally would cause events to disagree with the DB.

    Worker shutdown (pure cancel without wait_for timeout) is handled by
    F049's reclaim_stale, which puts orphaned 'running' rows back to
    'pending' for retry. So we trade a 'cancelled' telemetry row for
    consistency with the eventual DB state.
    """
    import asyncio

    runner = MagicMock()
    runner.run_turn = AsyncMock(side_effect=asyncio.CancelledError())
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask()
    emit = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await execute_hardened(
            subtask, "sess-cancel",
            runner=runner, heart=heart, settings=settings,
            emit_event=emit,
        )

    # Critical: NO persistence, NO emission on cancellation.
    heart.subtasks.complete.assert_not_called()
    heart.subtasks.fail.assert_not_called()
    emit.assert_not_called()
