"""F061: shared hardened-execution helper.

Used by BOTH the background subtask worker (``subtask_worker.py``) and the
inline ``await_result=True`` path in ``api/tools.py::spawn_task``. A single
executor closes the dominant silent-failure vector that the spec review
flagged: an inline path with no validator+retry would relocate the bug F061
exists to fix.

Contract:

- Caller is responsible for cleanup (``end_conversation``).
- Persistence is done HERE (via ``heart.subtasks.complete()`` or ``.fail()``)
  with the full ``final_outcome``, ``report_jsonb``, ``attempts``, and token
  counters populated. Worker/inline don't need to repeat that bookkeeping.
- On exception inside ``run_turn``, the per-attempt ``try/except`` ensures
  ``last_result`` is always non-None when ``_persist_outcome`` runs (the
  silent-failure reviewer's P1.3 finding).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from nous.api.subtask_tools import (
    SUBMIT_FINAL_REPORT_SCHEMA,
    SubtaskReportCollector,
    make_submit_final_report_executor,
)
from nous.api.tools import build_subtask_prefix
from nous.heart.subtask_validator import ValidationResult, validate_report

logger = logging.getLogger(__name__)


# F061 PR-3: structured outcome event for /dashboard/subtasks. Emitted via
# the EventBus to nous_system.events. Schema mirrors spec mechanism 10.
SUBTASK_OUTCOME_EVENT_TYPE = "subtask_outcome"


async def emit_outcome_event(
    bus,
    subtask,
    last_result: ValidationResult,
    last_payload: dict | None,
    *,
    settings,
    duration_ms: int | None = None,
    attempts: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tool_calls_made: int | None = None,
) -> None:
    """Emit a ``subtask_outcome`` event via the EventBus.

    Per silent-failure spec review P1.5: the entire body is wrapped in
    try/except + ``logger.exception``. Without inner error handling,
    fire-and-forget ``asyncio.create_task`` would silently drop the
    telemetry event on any DB / event-bus failure — recreating the exact
    silent-failure pattern F061 exists to eliminate.
    """
    if not settings.subtask_outcome_persistence_enabled:
        return
    if bus is None:
        return
    try:
        from nous.events import Event

        data: dict[str, Any] = {
            "subtask_id": str(subtask.id),
            "agent_id": getattr(subtask, "agent_id", None),
            "frame_type": getattr(subtask, "frame_type", None),
            "final_outcome": last_result.outcome,
            "ok": last_result.ok,
            "validator_reason": (
                last_result.reason if not last_result.ok else None
            ),
            "attempts": attempts,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_calls_made": tool_calls_made,
            "duration_ms": duration_ms,
            "dag_node_id": (
                str(subtask.dag_node_id)
                if getattr(subtask, "dag_node_id", None) is not None
                else None
            ),
        }
        await bus.emit(Event(
            type=SUBTASK_OUTCOME_EVENT_TYPE,
            agent_id=getattr(subtask, "agent_id", "") or settings.agent_id,
            session_id=f"subtask-{subtask.id.hex[:8]}",
            data=data,
        ))
    except Exception:
        logger.exception(
            "Failed to emit %s event for subtask %s",
            SUBTASK_OUTCOME_EVENT_TYPE, subtask.id.hex[:8],
        )


# Caller is responsible for ``end_conversation``; we just persist + emit telemetry.
EmitEventCallback = Callable[..., Awaitable[None]] | None
NotifyCallback = Callable[..., Awaitable[None]] | None


async def execute_hardened(
    subtask,
    session_id: str,
    *,
    runner,
    heart,
    settings,
    emit_event: EmitEventCallback = None,
    notify_telegram: NotifyCallback = None,
) -> tuple[str, ValidationResult]:
    """Run a subtask under the F061 contract.

    Returns ``(final_text, last_result)``. ``final_text`` is the report
    summary on success, the validator reason on failure, or the blocked
    reason on incomplete_blocked.
    """
    max_attempts: int = settings.subtask_max_attempts
    min_summary: int = settings.subtask_report_min_summary_chars

    collector = SubtaskReportCollector()
    extra_tools: dict[str, tuple[dict, Callable]] = {
        "submit_final_report": (
            SUBMIT_FINAL_REPORT_SCHEMA,
            make_submit_final_report_executor(collector),
        ),
    }
    output_format = subtask.output_format
    success_criteria = subtask.success_criteria
    system_prefix = build_subtask_prefix(
        subtask.task,
        subtask.frame_type,
        output_format=output_format,
        success_criteria=success_criteria,
        hardening_enabled=True,
    )

    user_message = subtask.task
    last_payload: dict[str, Any] | None = None
    # Initialize to a sentinel so _persist_outcome never sees None — addresses
    # the silent-failure review's P1.3 (AttributeError-masking-real-error).
    last_result: ValidationResult = ValidationResult.failed(
        "errored", "no attempts ran",
    )
    total_in = 0
    total_out = 0
    total_calls = 0
    attempt = 0  # populated in the loop; survives no-iter case via initial 0
    started_monotonic = time.monotonic()

    force_tool = (
        "submit_final_report"
        if settings.subtask_force_tool_on_penultimate
        else None
    )

    cancelled = False
    try:
        for attempt in range(1, max_attempts + 1):
            collector.reset()
            try:
                response_text, _ctx, usage = await runner.run_turn(
                    session_id=session_id,
                    user_message=user_message,
                    agent_id=settings.agent_id,
                    system_prompt_prefix=system_prefix,
                    skip_episode=True,
                    is_subtask=True,
                    max_tool_calls=settings.subtask_tool_call_limit,
                    model_override=subtask.model or settings.background_model,
                    is_background=True,
                    extra_tools=extra_tools,
                    force_tool_on_penultimate=force_tool,
                )
            except asyncio.CancelledError:
                # F061 PR-3 Codex review P1: do NOT classify CancelledError
                # as final_outcome="cancelled" inside this finally — both
                # worker and inline call sites wrap us in asyncio.wait_for
                # which converts a normal timeout into CancelledError before
                # re-raising as TimeoutError. If we persist+emit "cancelled"
                # here, the outer caller's TimeoutError branch will then
                # overwrite the DB row with "timed_out" but the EVENT row
                # is already locked in as "cancelled" — events disagree
                # with the DB. Instead: preserve last_result (sentinel or
                # prior-attempt value), let the finally write that, and
                # re-raise. The outer caller decides whether this was a
                # timeout (writes "timed_out") or a real shutdown cancel
                # (F049's reclaim_stale handles the orphaned "running" row).
                cancelled = True
                raise
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Subtask %s attempt %d errored", subtask.id.hex[:8], attempt,
                )
                last_result = ValidationResult.failed("errored", error_msg)
                break

            total_in += usage.get("input_tokens", 0)
            total_out += usage.get("output_tokens", 0)
            total_calls += usage.get("tool_calls", 0)

            last_payload = collector.get()
            last_result = validate_report(last_payload, min_summary_chars=min_summary)

            if last_result.ok or last_result.outcome == "incomplete_blocked":
                break
            if (
                attempt < max_attempts
                and last_result.outcome in {"incomplete_no_terminal", "validation_failed"}
            ):
                logger.warning(
                    "Subtask %s attempt %d rejected (%s: %s); retrying",
                    subtask.id.hex[:8], attempt, last_result.outcome, last_result.reason,
                )
                user_message = _build_retry_message(
                    subtask.task, last_payload, last_result.reason,
                )
                continue
            # Either out of attempts or non-recoverable outcome — exit.
            break
    finally:
        # Skip persist+emit on CancelledError. The outer caller wraps us in
        # asyncio.wait_for and decides the correct final_outcome:
        #   - wait_for timeout → outer catches TimeoutError → writes
        #     final_outcome="timed_out" itself
        #   - pure shutdown cancel → F049's reclaim_stale puts the orphaned
        #     "running" row back to "pending" for retry on next worker boot
        # Persisting from inside this finally on CancelledError would race
        # with the outer caller's overwrite (DB ends up correct but the
        # event is locked at the WRONG outcome — telemetry desyncs from DB).
        if not cancelled:
            try:
                await asyncio.shield(_persist_outcome(
                    heart,
                    subtask,
                    last_result,
                    last_payload,
                    attempts=attempt or 1,
                    tokens_in=total_in,
                    tokens_out=total_out,
                    tool_calls_made=total_calls,
                ))
            except Exception:
                logger.exception(
                    "Subtask %s _persist_outcome failed in finally",
                    subtask.id.hex[:8],
                )

            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            if emit_event:
                try:
                    await asyncio.shield(emit_event(
                        subtask, last_result, last_payload,
                        duration_ms=duration_ms,
                        attempts=attempt or 1,
                        tokens_in=total_in,
                        tokens_out=total_out,
                        tool_calls_made=total_calls,
                    ))
                except Exception:
                    logger.exception(
                        "Subtask %s emit_event failed in finally",
                        subtask.id.hex[:8],
                    )
        else:
            logger.info(
                "Subtask %s cancelled during execution; outer caller "
                "(asyncio.wait_for) will classify the final outcome",
                subtask.id.hex[:8],
            )
    if notify_telegram:
        try:
            await notify_telegram(subtask, last_result, last_payload)
        except Exception:  # pragma: no cover — defensive
            logger.exception(
                "Subtask %s notify_telegram failed", subtask.id.hex[:8],
            )

    if last_result.ok and last_result.report is not None:
        final_text = last_result.report.summary
    elif last_result.outcome == "incomplete_blocked":
        final_text = (
            (last_result.report.summary if last_result.report else "")
            or last_result.reason
        )
    else:
        final_text = (last_payload or {}).get("summary", "") or last_result.reason

    return final_text, last_result


def _build_retry_message(
    task: str,
    prior_payload: dict | None,
    reason: str,
) -> str:
    """Plain prose retry message — NEVER embedded JSON.

    Per silent-failure review P2.1: ``json.dumps(payload)[:2000]`` produces
    malformed JSON at truncation, confusing the model. Build a concise prose
    summary instead, hard-capped at 2000 chars to bound token spend.

    Defensive ``str(...)`` wraps because ``prior_payload`` is the RAW payload
    that just failed schema validation — it may contain non-string types
    (e.g., ``{"summary": 42, ...}``). Slicing an int raises ``TypeError``
    which would propagate out of execute_hardened and bypass _persist_outcome.

    The ``task`` parameter is reserved — current message focuses on the
    payload-level rejection. Task echoing is a future tuning option.
    """
    del task  # reserved for future task-echo behavior; see docstring.
    parts = [f"Your previous attempt was rejected: {reason}"]
    if prior_payload:
        prior_summary = str(prior_payload.get("summary") or "")[:300]
        prior_conf = prior_payload.get("confidence")
        if prior_summary:
            parts.append(f"Your previous summary (first 300 chars): {prior_summary}")
        if prior_conf is not None:
            parts.append(f"Your previous confidence: {prior_conf}")
    parts.append(
        "Try again. You MUST call submit_final_report with a schema-valid "
        "payload (summary >= 50 chars, no placeholder text, confidence 0-1)."
    )
    msg = "\n\n".join(parts)
    return msg if len(msg) <= 2000 else msg[:1997] + "..."


async def _persist_outcome(
    heart,
    subtask,
    last_result: ValidationResult,
    last_payload: dict | None,
    *,
    attempts: int,
    tokens_in: int,
    tokens_out: int,
    tool_calls_made: int,
) -> None:
    """Map ValidationResult onto a heart.subtasks row update."""
    common = dict(
        attempts=attempts,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tool_calls_made=tool_calls_made,
        report_jsonb=last_payload,
    )
    if last_result.ok and last_result.report is not None:
        await heart.subtasks.complete(
            subtask.id,
            last_result.report.summary,
            final_outcome="completed",
            **common,
        )
        return

    if last_result.outcome == "incomplete_blocked":
        # status='completed' (per spec mechanism 6) but final_outcome surfaces
        # the block. _format_subtask_results renders this as a separate
        # "Blocked Subtask" section; DAG _sync_subtask_node treats it as a
        # node failure (see PR-2 task #24).
        summary = (
            last_result.report.summary if last_result.report else last_result.reason
        )
        await heart.subtasks.complete(
            subtask.id,
            summary,
            final_outcome="incomplete_blocked",
            **common,
        )
        return

    # All remaining outcomes (incomplete_no_terminal, validation_failed,
    # errored, timed_out) → status='failed', error=reason.
    await heart.subtasks.fail(
        subtask.id,
        last_result.reason,
        final_outcome=last_result.outcome,
        **common,
    )
