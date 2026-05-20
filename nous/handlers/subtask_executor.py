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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import jsonschema

from nous.api.subtask_tools import (
    SubtaskReportCollector,
    build_submit_final_report_schema,
    make_submit_final_report_executor,
)
from nous.api.tools import build_subtask_prefix
from nous.heart.subtask_validator import ValidationResult, validate_report

logger = logging.getLogger(__name__)


@dataclass
class HardenedRunState:
    """Real-time state of an in-progress ``execute_hardened`` run.

    Outer callers (``subtask_worker._process_subtask``,
    ``tools.py::spawn_task`` inline path) construct one of these and pass
    it via the ``state=`` kwarg. The executor mutates it in-place at every
    attempt boundary so that — when the outer ``asyncio.wait_for`` fires
    a timeout and ``execute_hardened``'s in-flight attempt is cancelled —
    the outer caller can read accurate ``attempts``/token counts to
    persist the row and emit the ``subtask_outcome`` event.

    Without this side channel, the outer timeout handler had no way to
    know how many attempts the executor consumed, and used a hardcoded
    ``attempts=1`` that was wrong whenever attempt 2 was in progress.
    """

    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls_made: int = 0
    last_payload: dict[str, Any] | None = None


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
    state: HardenedRunState | None = None,
) -> tuple[str, ValidationResult]:
    """Run a subtask under the F061 contract.

    Returns ``(final_text, last_result)``. ``final_text`` is the report
    summary on success, the validator reason on failure, or the blocked
    reason on incomplete_blocked.
    """
    max_attempts: int = settings.subtask_max_attempts
    min_summary: int = settings.subtask_report_min_summary_chars

    collector = SubtaskReportCollector()
    # F062: when the row has a payload_schema AND the flag is on, expose
    # the optional `payload` property on submit_final_report so the model
    # can transport its typed payload through the tool-use round trip.
    # Otherwise stay byte-identical to F061's fail-closed schema. ``getattr``
    # with default=False keeps test-fixture SimpleNamespace settings working
    # (they don't carry the new flag) and prevents AttributeError on any
    # custom Settings subclass that omits the field.
    _payload_property_enabled = (
        getattr(settings, "subtask_payload_schema_enabled", False)
        and getattr(subtask, "payload_schema", None) is not None
    )
    extra_tools: dict[str, tuple[dict, Callable]] = {
        "submit_final_report": (
            build_submit_final_report_schema(_payload_property_enabled),
            make_submit_final_report_executor(collector),
        ),
    }
    output_format = subtask.output_format
    success_criteria = subtask.success_criteria
    # F062: pass caller-supplied payload_schema only when the flag is on.
    # The settings gate is enforced again at validation time; this gate keeps
    # the prompt clean when the executor is invoked with a stale row that
    # already has payload_schema set but the operator has since disabled
    # the flag. ``getattr`` keeps SimpleNamespace test fixtures working.
    payload_schema_for_prompt = (
        getattr(subtask, "payload_schema", None)
        if getattr(settings, "subtask_payload_schema_enabled", False)
        else None
    )
    system_prefix = build_subtask_prefix(
        subtask.task,
        subtask.frame_type,
        output_format=output_format,
        success_criteria=success_criteria,
        hardening_enabled=True,
        payload_schema=payload_schema_for_prompt,
    )

    user_message = subtask.task
    last_payload: dict[str, Any] | None = None
    # Initialize to a sentinel so _persist_outcome never sees None — addresses
    # the silent-failure review's P1.3 (AttributeError-masking-real-error).
    last_result: ValidationResult = ValidationResult.failed(
        "errored", "no attempts ran",
    )
    # F061 PR-3 round 4: HardenedRunState side channel. Outer caller
    # mutates ``state`` in-place at every attempt boundary so that on
    # outer wait_for timeout the timeout handler can read accurate
    # attempts/token counts (it has no other access to local state in
    # this coroutine after CancelledError).
    if state is None:
        state = HardenedRunState()
    # Caller is expected to pass a fresh state (or None); we don't reset
    # so the outer timeout handler can pre-seed values for testing or
    # for resumed executions in a future PR.
    total_in = state.tokens_in
    total_out = state.tokens_out
    total_calls = state.tool_calls_made
    attempt = state.attempts  # 0 if fresh
    # F062: tri-state — None when no schema check ran, True/False otherwise.
    # Reset at the start of EVERY attempt (Codex round-6 P2) so a False
    # from attempt 1 cannot leak into a later runtime-error outcome —
    # otherwise the persisted row would carry final_outcome='errored' with
    # payload_schema_valid=False, contradictory state for telemetry.
    payload_schema_valid: bool | None = None
    started_monotonic = time.monotonic()

    force_tool = (
        "submit_final_report"
        if settings.subtask_force_tool_on_penultimate
        else None
    )

    cancelled = False
    try:
        for attempt in range(1, max_attempts + 1):
            # Mirror to state BEFORE the attempt runs so an outer wait_for
            # timeout that fires during the run still sees attempt=N.
            state.attempts = attempt
            collector.reset()
            # F062 (Codex round-6 P2): reset per-attempt so a False from
            # the prior attempt cannot leak into a later non-schema
            # terminal outcome (e.g. attempt 1 schema-mismatch, attempt 2
            # API exception → row would otherwise be persisted with
            # final_outcome='errored' AND payload_schema_valid=False).
            payload_schema_valid = None
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
                    # F064.1: plumb dag_node_id through the hardened path
                    # too — without this, F064.1 stall pings never fire
                    # in production where subtask_hardening_enabled=true.
                    # @codex P1 on 2399032: only the legacy _execute_legacy
                    # path was carrying dag_node_id previously.
                    dag_node_id=getattr(subtask, "dag_node_id", None),
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
            # Mirror cumulative counts after each successful API call.
            state.tokens_in = total_in
            state.tokens_out = total_out
            state.tool_calls_made = total_calls

            last_payload = collector.get()
            state.last_payload = last_payload
            last_result = validate_report(last_payload, min_summary_chars=min_summary)

            # F062: post-structural-validation JSON Schema check on the
            # optional `payload` field of the report. Fires only when
            # (a) F061's structural validator just returned ok, (b) the
            # F062 master flag is on, and (c) the caller supplied a
            # payload_schema at spawn_sync time. Failure rewrites
            # last_result to validation_failed so F061's existing retry
            # loop handles it identically to a structural failure —
            # status-mirrors-final_outcome invariant preserved.
            if (
                last_result.ok
                and getattr(settings, "subtask_payload_schema_enabled", False)
                and getattr(subtask, "payload_schema", None) is not None
            ):
                # Codex round-4 P1: tool-call arguments are already parsed by
                # the runner into native Python values. Calling json.loads on
                # a string payload like "hello" (valid for {"type":"string"})
                # would mis-fire as JSONDecodeError and force validation_failed
                # on a legitimately schema-valid string/scalar value. Always
                # validate the raw Python value directly.
                raw_payload = (last_payload or {}).get("payload")
                try:
                    jsonschema.validate(raw_payload, subtask.payload_schema)
                    payload_schema_valid = True
                except jsonschema.ValidationError as e:
                    logger.warning(
                        "Subtask %s attempt %d payload schema mismatch: %s",
                        subtask.id.hex[:8], attempt, e,
                    )
                    last_result = ValidationResult.failed(
                        "validation_failed",
                        f"payload schema mismatch: {e}",
                    )
                    payload_schema_valid = False
                except jsonschema.SchemaError as e:
                    # Codex round-6 P2: malformed caller schema raises
                    # SchemaError, not ValidationError. Map to validation_failed
                    # too so spawn_sync callers see a deterministic outcome
                    # rather than the exception escaping into the generic
                    # "errored" path.
                    logger.warning(
                        "Subtask %s attempt %d caller-supplied payload_schema is malformed: %s",
                        subtask.id.hex[:8], attempt, e,
                    )
                    last_result = ValidationResult.failed(
                        "validation_failed",
                        f"payload_schema is malformed: {e}",
                    )
                    payload_schema_valid = False

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
                    min_summary_chars=min_summary,
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
                    payload_schema_valid=payload_schema_valid,
                ))
            except asyncio.CancelledError:
                # F061 PR-3 silent-failure review P1.1: CancelledError
                # derives from BaseException, not Exception, so the bare
                # ``except Exception`` would NOT catch it — and the
                # subsequent emit_event call would be skipped. Catch
                # explicitly, log loudly, then re-raise so cancellation
                # actually terminates the worker.
                logger.warning(
                    "Subtask %s _persist_outcome cancelled mid-finally "
                    "(shield preserves the inner Task; emit_event will "
                    "NOT run; downstream telemetry consumers may miss "
                    "this row's subtask_outcome event)",
                    subtask.id.hex[:8],
                )
                raise
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
                except asyncio.CancelledError:
                    logger.warning(
                        "Subtask %s emit_event cancelled mid-finally",
                        subtask.id.hex[:8],
                    )
                    raise
                except Exception:
                    logger.exception(
                        "Subtask %s emit_event failed in finally",
                        subtask.id.hex[:8],
                    )
        else:
            logger.info(
                "Subtask %s cancelled during execution; outer caller "
                "(asyncio.wait_for) will classify the final outcome and "
                "emit the subtask_outcome event using state=HardenedRunState",
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
    *,
    min_summary_chars: int,
) -> str:
    """Plain prose retry message — NEVER embedded JSON.

    Per silent-failure review P2.1: ``json.dumps(payload)[:2000]`` produces
    malformed JSON at truncation, confusing the model. Build a concise prose
    summary instead, hard-capped at 2000 chars to bound token spend.

    Defensive ``str(...)`` wraps because ``prior_payload`` is the RAW payload
    that just failed schema validation — it may contain non-string types
    (e.g., ``{"summary": 42, ...}``). Slicing an int raises ``TypeError``
    which would propagate out of execute_hardened and bypass _persist_outcome.

    Codex review (PR #421 commit c4d2396 follow-up): ``min_summary_chars``
    is now passed from the active runtime config rather than hardcoded so
    that operators raising ``NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS`` don't
    cause the model to retry against a stale 50-char target and hit a
    guaranteed ``validation_failed`` outcome.

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
        f"Try again. You MUST call submit_final_report with a schema-valid "
        f"payload (summary >= {min_summary_chars} chars, no placeholder text, "
        f"confidence 0-1)."
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
    payload_schema_valid: bool | None = None,
) -> None:
    """Map ValidationResult onto a heart.subtasks row update."""
    common = dict(
        attempts=attempts,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tool_calls_made=tool_calls_made,
        report_jsonb=last_payload,
        payload_schema_valid=payload_schema_valid,
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
