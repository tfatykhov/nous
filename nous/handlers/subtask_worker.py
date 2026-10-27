"""Subtask Worker Pool -- executes queued subtasks as independent agent turns.

Polls the subtask queue and runs each task via AgentRunner.run_turn().
On completion/failure, updates status, emits events, and optionally
sends Telegram notifications.

Workers are asyncio tasks that loop: dequeue -> execute -> repeat.
Concurrency is capped at subtask_max_concurrent to prevent resource
exhaustion.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nous.handlers.subtask_executor import HardenedRunState
from datetime import UTC, datetime
from uuid import UUID

import httpx

from nous.api.execution_context import ExecutionContext
from nous.brain import continuation, intentions
from nous.cancellation import cancel_requested
from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers import schedule_guard
from nous.heart.heart import Heart
from nous.heart.job_runs import FIX_KEY
from nous.heart.result_inbox import close_intention_quietly, record_subtask_result
from nous.storage.models import Subtask

logger = logging.getLogger(__name__)

# The notification is plain text (no parse_mode), so Telegram's 4096 limit applies
# to the UTF-16 units of the raw string; no HTML-escaping margin is needed.
_TELEGRAM_MAX_UNITS = 4096
_TASK_PREVIEW_UNITS = 100


def _error_text(exc: BaseException) -> str:
    """What the row of a subtask whose turn failed says about the failure.

    A CancelledError that reaches the turn's error handling came out of
    something the turn awaited (the worker's own cancellation is re-raised
    before this), so it gets a text that does not read like a stop.
    """
    if isinstance(exc, asyncio.CancelledError):
        return "CancelledError: a call inside the turn was cancelled; the worker was not stopped"
    return f"{type(exc).__name__}: {exc}"


class SubtaskWorkerPool:
    """Pool of asyncio workers that execute subtasks via AgentRunner.

    Each worker loops: dequeue a pending subtask -> run it as an agent
    turn -> mark complete/failed -> repeat.  Workers sleep when the
    queue is empty.
    """

    def __init__(
        self,
        runner: object,  # AgentRunner — typed loosely to avoid circular imports
        heart: Heart,
        settings: Settings,
        bus: EventBus | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._runner = runner
        self._heart = heart
        self._settings = settings
        self._bus = bus
        self._http = http_client
        self._workers: list[asyncio.Task] = []
        self._running = False
        # F061 round 4: per-subtask HardenedRunState so the outer
        # _process_subtask timeout handler can read accurate
        # attempts/token counts after asyncio.wait_for cancels the
        # in-flight execute_hardened. Keyed by subtask.id; populated
        # in _execute_subtask, consumed in _process_subtask, cleared
        # after every dispatch.
        self._inflight_state: dict[Any, "HardenedRunState"] = {}

    async def start(self) -> None:
        """Spawn worker tasks, after closing what a prior crash left behind.

        The inline subtasks it left running are cancelled first: reclaim_stale()
        would re-queue one past its timeout, and no worker may run an inline
        subtask.
        """
        await self._heart.subtasks.cancel_orphaned_inline()
        reclaimed = await self._heart.subtasks.reclaim_stale()
        if reclaimed:
            logger.info("Reclaimed %d stale subtasks on startup", reclaimed)

        self._running = True
        num_workers = self._settings.subtask_workers
        for i in range(num_workers):
            worker_id = f"worker-{i}"
            task = asyncio.create_task(
                self._worker_loop(worker_id), name=f"subtask-{worker_id}"
            )
            self._workers.append(task)
        logger.info(
            "Subtask worker pool started (%d workers, poll=%.1fs)",
            num_workers,
            self._settings.subtask_poll_interval,
        )

    async def stop(self) -> None:
        """Cancel all workers and wait for them to finish."""
        self._running = False
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        logger.info("Subtask worker pool stopped")

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self, worker_id: str) -> None:
        """Main loop for a single worker: poll, execute, repeat."""
        while self._running:
            try:
                subtask = await self._heart.subtasks.dequeue(worker_id)
                if subtask is None:
                    await asyncio.sleep(self._settings.subtask_poll_interval)
                    continue

                await self._process_subtask(subtask)

            except asyncio.CancelledError:
                if cancel_requested():
                    break
                logger.exception("Worker %s was cancelled from within — the worker continues", worker_id)
                await asyncio.sleep(self._settings.subtask_poll_interval)
            except Exception:
                logger.exception("Worker %s encountered unexpected error", worker_id)
                await asyncio.sleep(self._settings.subtask_poll_interval)

    # ------------------------------------------------------------------
    # Subtask processing
    # ------------------------------------------------------------------

    async def _process_subtask(self, subtask: Subtask) -> None:
        """Execute subtask with timeout protection, unless its schedule's guard completes it."""
        if await self._run_guard(subtask):
            return
        timeout = subtask.timeout_seconds or self._settings.subtask_default_timeout
        try:
            await asyncio.wait_for(
                self._execute_subtask(subtask),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # F061 PR-3 round 4: read the HardenedRunState side channel
            # (populated by execute_hardened in real time) so the timeout
            # row reflects accurate attempt count + token usage rather
            # than the previous hardcoded ``attempts=1`` (which was wrong
            # whenever the executor was on attempt 2 when wait_for fired).
            state = self._inflight_state.get(subtask.id)
            error_msg = f"Subtask timed out after {timeout}s"
            logger.warning(
                "Subtask %s timed out after %ds",
                subtask.id.hex[:8], timeout,
            )
            attempts = max(1, state.attempts) if state else 1
            tokens_in = state.tokens_in if state else 0
            tokens_out = state.tokens_out if state else 0
            tool_calls_made = state.tool_calls_made if state else 0
            await self._heart.subtasks.fail(
                subtask.id, error_msg, final_outcome="timed_out",
                attempts=attempts, tokens_in=tokens_in,
                tokens_out=tokens_out, tool_calls_made=tool_calls_made,
            )
            # F061 PR-3 Codex round 4: emit subtask_outcome event from
            # the outer handler. execute_hardened skips persist+emit on
            # CancelledError (avoids the round-1 race where it would
            # write 'cancelled' before this handler overwrites with
            # 'timed_out'); the outer handler is the authoritative
            # classification site, so it must also emit the event or
            # event-driven telemetry consumers (eval harness, audits)
            # silently miss timeouts.
            await self._emit_outcome_from_outer(
                subtask, "timed_out", error_msg,
                attempts=attempts, tokens_in=tokens_in,
                tokens_out=tokens_out, tool_calls_made=tool_calls_made,
            )
            await self._emit_event("subtask_failed", subtask, error=error_msg)
            await self._notify_telegram(subtask, error=error_msg)
        finally:
            self._inflight_state.pop(subtask.id, None)
            await self._record_inbox(subtask)

    async def _guard_spec(self, subtask: Subtask) -> schedule_guard.GuardSpec | None:
        """The guard of the schedule this run is a plain fire of, or None.

        The guard's name comes from this agent's schedule row at run time, never from
        the run's own metadata or task text (the run only names its schedule), and its
        command only from the owner's allowlist in Settings. A
        check-and-fix run, or any run whose text is not the schedule's task, is never
        guarded. A failed read means no guard.
        """
        if not self._settings.schedule_guards_enabled:
            return None
        meta = getattr(subtask, "metadata_", None)
        if not isinstance(meta, dict) or not meta.get("schedule_id") or FIX_KEY in meta:
            return None
        try:
            schedule = await self._heart.schedules.get(UUID(meta["schedule_id"]))
        except Exception:
            logger.warning("Could not read the schedule of subtask %s for its guard", subtask.id.hex[:8], exc_info=True)
            return None
        if schedule is None or schedule.agent_id != self._settings.agent_id or schedule.task != subtask.task:
            return None
        raw = (schedule.metadata_ or {}).get("guard")
        spec = schedule_guard.parse_guard(raw, self._settings.schedule_guard_commands)
        if spec is None and raw is not None:
            commands = self._settings.schedule_guard_commands
            logger.warning(
                "Schedule %s has a guard naming %r, which is not one of the %d NOUS_SCHEDULE_GUARD_COMMANDS "
                "entries %s; ignoring it",
                schedule.id, raw.get("name") if isinstance(raw, dict) else raw, len(commands), sorted(commands),
            )
        return spec

    async def _run_guard(self, subtask: Subtask) -> bool:
        """Run the schedule's guard, if any. True when it completed the run with no model call.

        Otherwise the guard's receipt is appended to the run's task text in memory
        (the row is not changed) and the model turn runs as usual: fail open. Never
        raises, except for cancellation.

        Every run of the command that returns a result leaves a durable record
        (``schedule_guard.record``): on a skip in ``report_jsonb.guard``, otherwise in a
        ``subtask_guard_ran`` event that the event bus persists (best effort: no bus, a
        full queue or a failed insert loses it, with a log line). A run cut short by a
        cancellation has no result and leaves only the log line.
        """
        sid = subtask.id.hex[:8]
        result: schedule_guard.GuardResult | None = None  # set only once spec is: the except below relies on it
        try:
            spec = await self._guard_spec(subtask)
            if spec is None:
                return False
            result = await schedule_guard.run_guard(spec)
            if not schedule_guard.should_skip(spec, result):
                # A script that could not run (126/127), was killed by a signal, or wrote to stderr
                # with exit 0 (which blocks every skip) is as broken as one that timed out.
                broken = (
                    result.error
                    or result.timed_out
                    or spec.skip_when != schedule_guard.SKIP_WHEN_EXIT0_QUIET
                    or result.exit_code in (126, 127)
                    or (result.exit_code is not None and result.exit_code < 0)
                    or (result.exit_code == 0 and bool(result.stderr.strip()))
                )
                (logger.warning if broken else logger.info)(
                    "Guard of subtask %s did not certify a no-op (exit=%s timed_out=%s error=%s skip_when=%s); "
                    "running the turn",
                    sid, result.exit_code, result.timed_out, result.error, spec.skip_when,
                )
                subtask.task += schedule_guard.receipt(spec, result)
                await self._record_guard_run(subtask, spec, result)
                return False
            text = schedule_guard.skip_text(spec, result)
            await self._heart.subtasks.complete(
                subtask.id, text,
                final_outcome="completed",
                report_jsonb={"guard_skipped": True, "guard": schedule_guard.record(spec, result, skipped=True)},
                attempts=0, tokens_in=0, tokens_out=0, tool_calls_made=0,
            )
        except (asyncio.CancelledError, Exception) as exc:
            if isinstance(exc, asyncio.CancelledError) and cancel_requested():
                raise
            logger.exception("Guard of subtask %s failed; running the turn", sid)
            if result is not None:  # the command ran, but the skip was not written
                await self._record_guard_run(subtask, spec, result)
            return False
        # The row is completed: from here on nothing may send the run to the model.
        try:
            await self._emit_event("subtask_guard_skipped", subtask, result=text)
            # No inbox row, memory or push for a run that did nothing, but its intention
            # still closes, as the inbox writer would have closed it.
            if getattr(self._settings, "result_inbox_enabled", False) is True:
                await close_intention_quietly(
                    self._heart.result_inbox, self._settings, intentions.SOURCE_SUBTASK, subtask.id
                )
        except Exception:
            logger.exception("Subtask %s: skipped by its guard, but its event or intention close failed", sid)
        logger.info("Subtask %s skipped by its schedule's guard", sid)
        return True

    async def _record_guard_run(
        self, subtask: Subtask, spec: schedule_guard.GuardSpec, result: schedule_guard.GuardResult
    ) -> None:
        """Persist the record of a guard run that did not skip the run. Never raises."""
        try:
            await self._emit_event(
                "subtask_guard_ran", subtask, extra={"guard": schedule_guard.record(spec, result, skipped=False)}
            )
        except Exception:
            logger.exception("Subtask %s: could not record its guard run", subtask.id.hex[:8])

    async def _record_inbox(self, subtask: Subtask) -> None:
        """F098: queue the finished result for the conversation that spawned it,
        and (Phase C) write it to memory.

        One site for every worker path that runs a turn (hardened, legacy,
        timeout); a run its schedule's guard skipped never comes here. The row
        is re-read once so both writers see the status that was actually
        committed. Inline subtasks never come through here — their result is
        returned in the calling turn. The two writers are independent: each
        swallows its own errors. Never raises.
        """
        inbox = getattr(self._settings, "result_inbox_enabled", False) is True
        memory = getattr(self._settings, "result_memory_enabled", False) is True
        if not inbox and not memory:
            return
        try:
            row = await self._heart.subtasks.get(subtask.id)
        except Exception:
            logger.warning("F098: could not re-read subtask %s for the inbox", subtask.id.hex[:8], exc_info=True)
            return
        if inbox:
            await record_subtask_result(self._heart.result_inbox, row, self._settings)
        if memory:
            await self._heart.result_memory.record(row)

    async def _execute_subtask(self, subtask: Subtask) -> None:
        """Run the subtask as an agent turn via AgentRunner.

        F061: routes to ``execute_hardened`` (with forced terminal tool +
        validator + retry) when ``subtask_hardening_enabled``; otherwise
        runs the legacy path unchanged.
        """
        # F064.5 v1: respect a caller-supplied session_id (e.g. the
        # continuation pattern from task_scheduler) so the runner reuses
        # the existing Episode via Episode.session_id contract. Fallback
        # remains the per-subtask unique session_id for non-continuation
        # paths. getattr-with-default keeps existing test mocks
        # (SimpleNamespace without metadata_) working.
        meta = getattr(subtask, "metadata_", None) or {}
        override = meta.get("session_id") if isinstance(meta, dict) else None
        session_id = override if override else f"subtask-{subtask.id.hex[:8]}"
        logger.info(
            "Executing subtask %s: %s",
            subtask.id.hex[:8],
            subtask.task[:80],
        )

        try:
            if self._settings.subtask_hardening_enabled:
                # Late import: nous.handlers.subtask_executor imports
                # build_subtask_prefix from nous.api.tools, which imports
                # this module — top-level import would deadlock at startup.
                # (functools.partial is stdlib and circular-safe; imported
                # at module top.)
                from nous.handlers.subtask_executor import (
                    HardenedRunState,
                    emit_outcome_event,
                    execute_hardened,
                )

                # F061 PR-3: pass an emit_event callback that forwards to
                # the bus-level outcome event with full telemetry payload.
                _outcome_emitter = partial(
                    emit_outcome_event, self._bus, settings=self._settings,
                )

                # F061 round 4: HardenedRunState side channel for outer
                # timeout handler in _process_subtask to read accurate
                # attempts + tokens after wait_for cancels execute_hardened.
                state = HardenedRunState()
                self._inflight_state[subtask.id] = state

                # F061 PR-3 round 4 P1-B: ``executed`` flag mirrors the
                # inline-path pattern. If post-execute_hardened code
                # (notify_telegram, response shaping) raises, the outer
                # ``except Exception`` must NOT call fail() again — the row
                # is already persisted by execute_hardened._persist_outcome.
                executed = False
                try:
                    final_text, _result = await execute_hardened(
                        subtask, session_id,
                        runner=self._runner,
                        heart=self._heart,
                        settings=self._settings,
                        emit_event=_outcome_emitter,
                        state=state,
                    )
                    executed = True
                    # Telemetry parity with the legacy path. Persistence is
                    # already done inside execute_hardened.
                    if _result.ok:
                        await self._emit_event(
                            "subtask_completed", subtask, result=final_text,
                        )
                        await self._notify_telegram(subtask, result=final_text)
                    else:
                        await self._emit_event(
                            "subtask_failed", subtask,
                            error=f"{_result.outcome}: {_result.reason}",
                        )
                        await self._notify_telegram(
                            subtask,
                            error=f"{_result.outcome}: {_result.reason}",
                        )
                    logger.info(
                        "Subtask %s done outcome=%s",
                        subtask.id.hex[:8], _result.outcome,
                    )
                except (asyncio.CancelledError, Exception) as exc:
                    if isinstance(exc, asyncio.CancelledError) and cancel_requested():
                        raise
                    # A CancelledError that gets here while nobody is stopping
                    # this worker came out of something the turn awaited: a
                    # failed subtask, like any other error.
                    # execute_hardened catches per-attempt exceptions, but
                    # defensive: a programmer error in execute_hardened itself
                    # must still surface visibly. ``executed`` flag prevents
                    # double-persist when the exception happened AFTER
                    # execute_hardened's _persist_outcome already wrote the
                    # row (e.g., post-finally response-formatting code).
                    error_msg = _error_text(exc)
                    logger.exception(
                        "Subtask %s hardened-path errored",
                        subtask.id.hex[:8],
                    )
                    if not executed:
                        attempts = max(1, state.attempts)
                        await self._heart.subtasks.fail(
                            subtask.id, error_msg, final_outcome="errored",
                            attempts=attempts,
                            tokens_in=state.tokens_in,
                            tokens_out=state.tokens_out,
                            tool_calls_made=state.tool_calls_made,
                        )
                        # P1-D: emit outcome event from outer handler
                        # since execute_hardened didn't reach its emit.
                        await self._emit_outcome_from_outer(
                            subtask, "errored", error_msg,
                            attempts=attempts,
                            tokens_in=state.tokens_in,
                            tokens_out=state.tokens_out,
                            tool_calls_made=state.tool_calls_made,
                        )
                    await self._emit_event("subtask_failed", subtask, error=error_msg)
                    await self._notify_telegram(subtask, error=error_msg)
            else:
                await self._execute_legacy(subtask, session_id)
        finally:
            # F049 Mechanism B: guarantee session teardown on every exit path.
            cleanup_timeout = self._settings.subtask_cleanup_timeout_seconds
            try:
                await asyncio.shield(
                    asyncio.wait_for(
                        self._runner.end_conversation(
                            session_id, agent_id=self._settings.agent_id
                        ),
                        timeout=cleanup_timeout,
                    )
                )
                logger.debug("Ended subtask session %s", session_id)
            except TimeoutError:
                logger.error(
                    "Subtask cleanup timed out after %ds for session %s — possible runner/brain outage",
                    cleanup_timeout, session_id,
                )
            except asyncio.CancelledError:
                logger.warning("Subtask cleanup cancelled for %s", session_id)
                raise
            except Exception:
                logger.exception(
                    "Subtask cleanup failed for session %s — end_conversation raised",
                    session_id,
                )

    async def _execute_legacy(self, subtask: Subtask, session_id: str) -> None:
        """Pre-F061 execution path. Removed in PR-6 once flag is locked on."""
        from nous.api.tools import build_subtask_prefix

        system_prefix = build_subtask_prefix(subtask.task, subtask.frame_type)
        from nous.handlers.job_lessons import job_lessons_block

        block = await job_lessons_block(self._heart, self._settings, subtask)
        if block:
            system_prefix += "\n\n" + block

        # F064.1: surface dag_node_id (populated by F061 PR-3 from
        # orchestrator._launch_subtask_node) so the runner can fire activity
        # pings. None for non-DAG subtasks — the ping helper is no-op safe.
        _dag_node_id = getattr(subtask, "dag_node_id", None)

        try:
            response_text, _turn_ctx, usage = await self._runner.run_turn(
                session_id=session_id,
                user_message=subtask.task,
                agent_id=self._settings.agent_id,
                system_prompt_prefix=system_prefix,
                skip_episode=True,
                is_subtask=True,
                max_tool_calls=self._settings.subtask_tool_call_limit,
                model_override=subtask.model or self._settings.background_model,
                is_background=True,
                dag_node_id=_dag_node_id,
                context=ExecutionContext.for_subtask(subtask, session_id),  # harness Phase 1a
            )
            # F061 PR-1: record outcome on legacy path so dashboard rows
            # are never NULL between PR-1 ship and PR-2 hardened-executor ship.
            # F061 round 4 P2-F: attempts=1 because legacy path runs
            # exactly one run_turn invocation (no retry loop).
            # F087 (@codex P2 on 2ccc026): persist the turn's token usage. It
            # was received and discarded, so tokens_in/tokens_out stayed 0 on
            # this path — and since subtask_hardening_enabled defaults FALSE,
            # this is the DEFAULT path. F087's DAG roll-up reads exactly these
            # columns, so without this the whole accounting feature records
            # zero in the default configuration (and, because the claim is
            # one-shot, freezes that zero permanently).
            usage = usage or {}
            await self._heart.subtasks.complete(
                subtask.id, response_text,
                final_outcome="completed", attempts=1,
                tokens_in=usage.get("input_tokens", 0),
                tokens_out=usage.get("output_tokens", 0),
                tool_calls_made=usage.get("tool_calls", 0),
            )
            await self._emit_event("subtask_completed", subtask, result=response_text)
            await self._notify_telegram(subtask, result=response_text)
            logger.info("Subtask %s completed", subtask.id.hex[:8])
        except (asyncio.CancelledError, Exception) as exc:
            if isinstance(exc, asyncio.CancelledError) and cancel_requested():
                raise
            # A CancelledError that gets here while nobody is stopping this
            # worker came out of something the turn awaited: a failed
            # subtask, like any other error.
            error_msg = _error_text(exc)
            logger.exception("Subtask %s failed", subtask.id.hex[:8])
            await self._heart.subtasks.fail(
                subtask.id, error_msg, final_outcome="errored", attempts=1,
            )
            await self._emit_event("subtask_failed", subtask, error=error_msg)
            await self._notify_telegram(subtask, error=error_msg)

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    async def _emit_outcome_from_outer(
        self,
        subtask: Subtask,
        final_outcome: str,
        reason: str,
        *,
        attempts: int,
        tokens_in: int,
        tokens_out: int,
        tool_calls_made: int,
    ) -> None:
        """Emit a ``subtask_outcome`` event from the outer timeout/exception
        handler — needed because ``execute_hardened`` skips persist+emit on
        CancelledError to avoid the round-1 race. Without this, event-driven
        telemetry consumers (eval harness, audits) silently miss timeouts
        and post-execute_hardened errors.

        Inner ``try/except + logger.exception`` mirrors ``emit_outcome_event``
        so a failed emit is loud, not silent. Skips when bus or persistence
        flag is disabled.
        """
        if self._bus is None:
            return
        if not self._settings.subtask_outcome_persistence_enabled:
            return
        try:
            from nous.handlers.subtask_executor import (
                SUBTASK_OUTCOME_EVENT_TYPE,
            )

            data: dict[str, Any] = {
                "subtask_id": str(subtask.id),
                "agent_id": getattr(subtask, "agent_id", None),
                "frame_type": getattr(subtask, "frame_type", None),
                "final_outcome": final_outcome,
                "ok": False,
                "validator_reason": reason,
                "attempts": attempts,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tool_calls_made": tool_calls_made,
                "duration_ms": None,  # outer handler doesn't have this
                "dag_node_id": (
                    str(subtask.dag_node_id)
                    if getattr(subtask, "dag_node_id", None) is not None
                    else None
                ),
            }
            await self._bus.emit(Event(
                type=SUBTASK_OUTCOME_EVENT_TYPE,
                agent_id=getattr(subtask, "agent_id", "") or self._settings.agent_id,
                session_id=f"subtask-{subtask.id.hex[:8]}",
                data=data,
            ))
        except Exception:
            logger.exception(
                "Failed to emit subtask_outcome from outer handler for %s",
                subtask.id.hex[:8],
            )

    async def _emit_event(
        self,
        event_type: str,
        subtask: Subtask,
        *,
        result: str | None = None,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit a subtask lifecycle event on the bus. ``extra`` is merged into its data."""
        if self._bus is None:
            return

        data: dict = {
            "subtask_id": subtask.id.hex,
            "task": subtask.task[:200],
        }
        if result is not None:
            data["result"] = result[:500]
        if error is not None:
            data["error"] = error[:500]
        if extra:
            data.update(extra)

        await self._bus.emit(Event(
            type=event_type,
            agent_id=self._settings.agent_id,
            session_id=f"subtask-{subtask.id.hex[:8]}",
            data=data,
            trace_id=getattr(subtask, "_trace_id", None),       # F035.2: propagate if set
            caused_by=getattr(subtask, "_caused_by", None),     # F035.2: propagate if set
        ))

    # ------------------------------------------------------------------
    # Telegram notifications
    # ------------------------------------------------------------------

    async def _superseded_by_continuation(self, subtask: Subtask) -> bool:
        """F099 Phase 2: a ``continue`` result goes to Nous's own continuation, so the raw push
        stands down (spec 4.3 item 5). So does the push of a subtask whose root the owner cancelled
        (2e final review m1): the cancel moves every open intention of the lineage to ``cancelled``,
        and the worker's own ``complete()`` was then a no-op. One point read, only for a
        ``notify=True`` subtask with the flag on (with it off no root is ever cancelled). A failed
        read sends the push: a duplicate costs less than a lost notification."""
        if not continuation.enabled(self._settings):
            return False
        store = getattr(self._heart, "intentions", None)
        if store is None:
            return False
        try:
            row = await store.get_for_source(intentions.SOURCE_SUBTASK, subtask.id)
        except Exception:
            logger.warning(
                "F099: could not read the intention of subtask %s; sending its push", subtask.id.hex[:8], exc_info=True
            )
            return False
        return row is not None and (
            row.wake_policy == intentions.WAKE_CONTINUE or row.state == continuation.STATE_CANCELLED
        )

    async def _notify_telegram(
        self,
        subtask: Subtask,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        """Send Telegram notification if configured and subtask has notify=True."""
        if not subtask.notify:
            return

        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
            return
        if await self._superseded_by_continuation(subtask):
            return

        task = continuation.clip_text(subtask.task, _TASK_PREVIEW_UNITS)
        if result is not None:
            head, body, what = f"Subtask completed: {task}\n\nResult: ", result, "full result"
        elif error is not None:
            head, body, what = f"Subtask failed: {task}\n\nError: ", error, "full error"
        else:
            return
        marker = f"\u2026 [truncated \u2014 {{dropped}} more chars; {what}: subtask {subtask.id.hex[:8]}]"
        room = _TELEGRAM_MAX_UNITS - continuation.utf16_units(head)
        text = head + continuation.clip_text(body, room, marker=marker)

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            client = self._http or httpx.AsyncClient()
            try:
                await client.post(
                    url,
                    json={"chat_id": chat_id, "text": text},
                    timeout=10,
                )
            finally:
                if self._http is None:
                    await client.aclose()
        except Exception:
            logger.warning("Telegram notification failed for subtask %s", subtask.id.hex[:8])
