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

import httpx

from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart.heart import Heart
from nous.storage.models import Subtask

logger = logging.getLogger(__name__)


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
        """Spawn worker tasks and reclaim any stale subtasks from prior crash."""
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
                break
            except Exception:
                logger.exception("Worker %s encountered unexpected error", worker_id)
                await asyncio.sleep(self._settings.subtask_poll_interval)

    # ------------------------------------------------------------------
    # Subtask processing
    # ------------------------------------------------------------------

    async def _process_subtask(self, subtask: Subtask) -> None:
        """Execute subtask with timeout protection."""
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
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # execute_hardened catches per-attempt exceptions, but
                    # defensive: a programmer error in execute_hardened itself
                    # must still surface visibly. ``executed`` flag prevents
                    # double-persist when the exception happened AFTER
                    # execute_hardened's _persist_outcome already wrote the
                    # row (e.g., post-finally response-formatting code).
                    error_msg = f"{type(exc).__name__}: {exc}"
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

        # F064.1: surface dag_node_id (populated by F061 PR-3 from
        # orchestrator._launch_subtask_node) so the runner can fire activity
        # pings. None for non-DAG subtasks — the ping helper is no-op safe.
        _dag_node_id = getattr(subtask, "dag_node_id", None)

        try:
            response_text, _turn_ctx, _usage = await self._runner.run_turn(
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
            )
            # F061 PR-1: record outcome on legacy path so dashboard rows
            # are never NULL between PR-1 ship and PR-2 hardened-executor ship.
            # F061 round 4 P2-F: attempts=1 because legacy path runs
            # exactly one run_turn invocation (no retry loop).
            await self._heart.subtasks.complete(
                subtask.id, response_text,
                final_outcome="completed", attempts=1,
            )
            await self._emit_event("subtask_completed", subtask, result=response_text)
            await self._notify_telegram(subtask, result=response_text)
            logger.info("Subtask %s completed", subtask.id.hex[:8])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
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
    ) -> None:
        """Emit a subtask lifecycle event on the bus."""
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

        if result is not None:
            text = f"Subtask completed: {subtask.task[:100]}\n\nResult: {result[:500]}"
        elif error is not None:
            text = f"Subtask failed: {subtask.task[:100]}\n\nError: {error[:300]}"
        else:
            return

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
