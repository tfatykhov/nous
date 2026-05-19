"""Session Timeout Monitor — detects idle sessions and triggers lifecycle events.

Tracks last activity per session. Actions:
- session idle > session_idle_timeout: routes through runner.end_conversation
  (canonical path, same as manual /new). Falls back to cognitive.end_session
  if runner is not wired yet (test fixtures, early startup).
- global idle > sleep_timeout: emits sleep_started

This solves the problem that most sessions never explicitly end —
users just stop talking. Without this, episode summaries, fact extraction,
and reflection never trigger.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from nous.config import Settings
from nous.events import Event, EventBus

if TYPE_CHECKING:
    from nous.api.runner import AgentRunner
    from nous.heart.heart import Heart

logger = logging.getLogger(__name__)


class SessionTimeoutMonitor:
    """Monitors session activity and triggers timeout actions.

    Runs a periodic check (every sleep_check_interval seconds) that:
    1. Finds sessions idle > session_idle_timeout -> calls cognitive.end_session()
    2. If global idle > sleep_timeout -> emits sleep_started

    P0-10 fix: The monitor calls cognitive.end_session() instead of emitting
    raw session_ended events. This ensures the timeout path has episode_id,
    transcript, and does full cleanup (episode ending, metadata, reflection).

    P1-4 fix: Also registers on session_ended to clean tracking dicts when
    sessions end explicitly.

    Idle-closure parity fix: when ``runner`` is wired, the timeout path calls
    ``runner.end_conversation`` (canonical /new flow) instead of going straight
    to ``cognitive.end_session``. That single seam decrements
    ``runner._conversations`` (which feeds /status memory.active_conversations),
    drops ledgers/compaction locks/pending corrections, resets the cache-break
    detector, deletes the persisted conversation_state row, and generates the
    reflection that yields "learned:" facts. Without it the manual /new path
    and the idle path diverge — the UI stays populated and reflection-derived
    facts never land.
    """

    def __init__(
        self,
        bus: EventBus,
        settings: Settings,
        *,
        cognitive: object | None = None,
        heart: Heart | None = None,
        runner: AgentRunner | None = None,
    ):
        self._bus = bus
        self._settings = settings
        self._cognitive = cognitive  # CognitiveLayer reference for end_session
        self._heart = heart  # F049: required for WM TTL sweep; None disables
        # AgentRunner reference for canonical end_conversation path.
        # Late-bound in main.py because runner is created after this monitor
        # (mirrors cognitive._monitor._procedure_learner wiring).
        self._runner = runner
        self._last_activity: dict[str, float] = {}  # session_id -> monotonic time
        self._last_agent: dict[str, str] = {}  # session_id -> agent_id
        self._global_last_activity: float = time.monotonic()
        self._sleep_emitted: bool = False
        self._last_wm_sweep: float = 0.0  # F049: monotonic time of last WM sweep
        self._task: asyncio.Task | None = None

        bus.on("turn_completed", self.on_activity)
        bus.on("message_received", self.on_activity)
        bus.on("session_ended", self._on_session_ended)  # P1-4 fix: cleanup

    async def on_activity(self, event: Event) -> None:
        """Track session activity."""
        now = time.monotonic()
        if event.session_id:
            self._last_activity[event.session_id] = now
            if event.agent_id:
                self._last_agent[event.session_id] = event.agent_id
        self._global_last_activity = now
        self._sleep_emitted = False  # Reset sleep flag on any activity

    async def _on_session_ended(self, event: Event) -> None:
        """Clean up tracking when session ends (explicit or timeout). P1-4 fix."""
        if event.session_id:
            self._last_activity.pop(event.session_id, None)
            self._last_agent.pop(event.session_id, None)

    async def start(self) -> None:
        """Start the periodic timeout checker."""
        self._task = asyncio.create_task(self._check_loop(), name="session-monitor")
        logger.info(
            "Session timeout monitor started (idle=%ds, sleep=%ds)",
            self._settings.session_idle_timeout,
            self._settings.sleep_timeout,
        )

    async def stop(self) -> None:
        """Stop the monitor."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _check_loop(self) -> None:
        """Periodic check for idle sessions."""
        while True:
            try:
                await asyncio.sleep(self._settings.sleep_check_interval)
                await self._check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Session monitor check failed")

    async def _close_expired(self, session_id: str, agent_id: str) -> None:
        """Dispatch a single timed-out session closure on the canonical path.

        Used by _check_timeouts to fan out closures concurrently via gather.
        Exceptions propagate; the caller logs them per-future to keep error
        isolation (one stuck reflection cannot stop the others).
        """
        if self._runner is not None:
            await self._runner.end_conversation(session_id, agent_id=agent_id)
        elif self._cognitive is not None:
            await self._cognitive.end_session(
                agent_id=agent_id,
                session_id=session_id,
                reflection=None,
            )
        else:
            # Both unwired — should be unreachable in prod (main.py wires
            # cognitive at construction and runner immediately after).
            # Logged loudly so a future refactor that breaks the wiring
            # surfaces a signal instead of silently leaking sessions.
            logger.error(
                "Session %s expired but no runner/cognitive available — "
                "no cleanup performed (check main.py wiring)",
                session_id,
            )

    async def _check_timeouts(self) -> None:
        """Check all tracked sessions for timeouts."""
        now = time.monotonic()

        # 1. Collect expired sessions and dispatch closures concurrently.
        # Prefer runner.end_conversation when wired — this is the canonical
        # /new path (reflection LLM, cognitive.end_session, _conversations/
        # _ledgers/cache-break pops, persisted conversation_state delete).
        # Fall back to cognitive.end_session when runner is unavailable
        # (test fixtures, very early startup).
        #
        # Concurrency: each closure issues a reflection LLM call (~15-30s).
        # Serializing them would stall the next tick — and starve F049 WM
        # sweep + sleep_started detection — by the cumulative reflection
        # time when multiple sessions expire in the same tick. asyncio.gather
        # bounds the wait to the slowest single closure.
        expired_pairs: list[tuple[str, str]] = []
        for session_id, last in list(self._last_activity.items()):
            idle_seconds = now - last
            if idle_seconds > self._settings.session_idle_timeout:
                agent_id = self._last_agent.get(
                    session_id, self._settings.agent_id
                )
                logger.info(
                    "Session %s idle for %ds, triggering end_session",
                    session_id,
                    int(idle_seconds),
                )
                expired_pairs.append((session_id, agent_id))

        if expired_pairs:
            results = await asyncio.gather(
                *(
                    self._close_expired(sid, aid)
                    for sid, aid in expired_pairs
                ),
                return_exceptions=True,
            )
            # return_exceptions=True captures CancelledError too. If any
            # child was cancelled (monitor shutdown via stop() -> task.cancel()),
            # we MUST re-raise so _check_loop sees the cancel and breaks the
            # loop. Otherwise shutdown hangs until process kill.
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise result
            for (sid, _), result in zip(expired_pairs, results):
                if isinstance(result, BaseException):
                    # Failed closure: log + move on. Tracking-dict cleanup
                    # below runs unconditionally — the F060 abandoned-episode
                    # sleep phase is the backstop that recovers any episode
                    # state that this raise left dangling.
                    logger.exception(
                        "Failed to end timed-out session %s",
                        sid,
                        exc_info=result,
                    )

        for sid, _ in expired_pairs:
            self._last_activity.pop(sid, None)
            self._last_agent.pop(sid, None)

        # 2. Check global inactivity -> sleep_started
        #
        # Previous bug (#160): required `not self._last_activity` (zero tracked
        # sessions). But session_idle_timeout (30min) << sleep_timeout (2hr), so
        # by the time 2hr of global inactivity passes, all sessions are already
        # timed out and popped from the dict. The check was redundant in theory
        # but failed in practice because any new message within the 90-minute
        # gap between session timeout and sleep timeout would reset
        # _global_last_activity, preventing sleep from ever firing.
        #
        # Fix: check that all remaining tracked sessions are idle beyond
        # sleep_timeout, rather than requiring the dict to be empty. This
        # handles both the normal case (dict already empty after session
        # timeouts) and the edge case where sleep_timeout <= session_idle_timeout.
        global_idle = now - self._global_last_activity
        all_sessions_sleeping = all(
            (now - last) > self._settings.sleep_timeout
            for last in self._last_activity.values()
        ) if self._last_activity else True
        if (
            global_idle > self._settings.sleep_timeout
            and not self._sleep_emitted
            and all_sessions_sleeping
        ):
            logger.info("Global idle for %ds, emitting sleep_started", int(global_idle))
            _sleep_event = Event(
                type="sleep_started",
                agent_id="system",
                data={"idle_seconds": int(global_idle)},
            )
            _sleep_event.trace_id = _sleep_event.event_id  # Root event
            await self._bus.emit(_sleep_event)
            self._sleep_emitted = True

        # 3. F049: periodic WM TTL safety-net sweep.
        if (
            self._heart is not None
            and self._settings.working_memory_ttl_hours > 0
        ):
            sweep_interval = self._settings.working_memory_sweep_interval_seconds
            if now - self._last_wm_sweep >= sweep_interval:
                try:
                    await self._heart.working_memory.cleanup_stale(
                        max_age_hours=self._settings.working_memory_ttl_hours,
                        batch_size=self._settings.working_memory_sweep_batch_size,
                    )
                except Exception:
                    logger.exception("WM TTL sweep raised")
                finally:
                    self._last_wm_sweep = time.monotonic()

    def get_stats(self) -> dict:
        """F035.1: Return session monitor statistics."""
        now = time.monotonic()
        sessions = {}
        for sid, last in self._last_activity.items():
            sessions[sid] = {"idle_seconds": round(now - last, 1)}
        return {
            "tracked_sessions": len(self._last_activity),
            "sessions": sessions,
            "sleep_emitted": self._sleep_emitted,
            "global_idle_seconds": round(now - self._global_last_activity, 1),
        }
