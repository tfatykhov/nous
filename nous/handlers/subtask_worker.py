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
            error_msg = f"Subtask timed out after {timeout}s"
            logger.warning(
                "Subtask %s timed out after %ds",
                subtask.id.hex[:8],
                timeout,
            )
            await self._heart.subtasks.fail(subtask.id, error_msg)
            await self._emit_event("subtask_failed", subtask, error=error_msg)
            await self._notify_telegram(subtask, error=error_msg)

    async def _execute_subtask(self, subtask: Subtask) -> None:
        """Run the subtask as an agent turn via AgentRunner."""
        session_id = f"subtask-{subtask.id.hex[:8]}"
        logger.info(
            "Executing subtask %s: %s",
            subtask.id.hex[:8],
            subtask.task[:80],
        )

        # 012.2: Use shared prefix builder for frame-aware context
        from nous.api.tools import build_subtask_prefix

        system_prefix = build_subtask_prefix(subtask.task, subtask.frame_type)

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
            )

            await self._heart.subtasks.complete(subtask.id, response_text)
            await self._emit_event("subtask_completed", subtask, result=response_text)
            await self._notify_telegram(subtask, result=response_text)

            logger.info("Subtask %s completed", subtask.id.hex[:8])

        except asyncio.CancelledError:
            raise  # Propagate cancellation
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("Subtask %s failed", subtask.id.hex[:8])
            await self._heart.subtasks.fail(subtask.id, error_msg)
            await self._emit_event("subtask_failed", subtask, error=error_msg)
            await self._notify_telegram(subtask, error=error_msg)

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

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
