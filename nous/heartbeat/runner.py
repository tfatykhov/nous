"""Heartbeat runner — background tick loop with triage (F034).

Follows the TaskScheduler start/stop pattern: creates an asyncio.Task
that runs a periodic loop, checking due checks and triaging findings.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx

from nous.api.runner import AgentRunner
from nous.brain import Brain
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart import Heart
from nous.heartbeat.registry import CheckRegistry
from nous.heartbeat.schemas import CheckResult, Finding, HeartbeatResult

logger = logging.getLogger(__name__)


class HeartbeatRunner:
    """Background heartbeat loop with check execution and triage.

    Runs due checks on each tick, collects findings, and either
    sends Telegram notifications (high urgency) or opens a cognitive
    session (normal urgency) for the agent to process.
    """

    def __init__(
        self,
        settings: Settings,
        registry: CheckRegistry,
        runner: AgentRunner,
        brain: Brain,
        heart: Heart,
        bus: EventBus | None,
        http_client: httpx.AsyncClient | None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._brain = brain
        self._heart = heart
        self._bus = bus
        self._http = http_client

        self._task: asyncio.Task | None = None
        self._running = False
        self._tokens_used_today: int = 0
        self._budget_date: date = date.today()
        self._last_tick: datetime | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the heartbeat loop."""
        self._running = True
        await self._detect_missed_checks()
        self._task = asyncio.create_task(self._loop(), name="heartbeat-runner")
        logger.info(
            "F034: Heartbeat started (tick=%ds, quiet=%d-%d, budget=%d tokens/day)",
            self._settings.heartbeat_tick_interval,
            self._settings.heartbeat_quiet_start,
            self._settings.heartbeat_quiet_end,
            self._settings.heartbeat_daily_token_budget,
        )

    async def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("F034: Heartbeat stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main tick loop — runs until cancelled."""
        while self._running:
            try:
                await asyncio.sleep(self._settings.heartbeat_tick_interval)
                self._maybe_reset_budget()

                if self._in_quiet_hours():
                    # Still run urgent-override checks during quiet hours
                    await self._tick(urgent_only=True)
                else:
                    await self._tick()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Heartbeat tick failed")

    async def _tick(self, urgent_only: bool = False) -> list[Finding]:
        """Run due checks and triage findings."""
        now = datetime.now(UTC)
        due_checks = self._registry.get_due_checks(now)

        if urgent_only:
            due_checks = [c for c in due_checks if c.urgent_override]

        if not due_checks:
            return []

        all_findings: list[Finding] = []

        for check in due_checks:
            try:
                result: CheckResult = await asyncio.wait_for(
                    check.run(),
                    timeout=check.timeout,
                )
                check.mark_success()

                if result.has_updates:
                    for f in result.findings:
                        f.check_name = check.name
                    all_findings.extend(result.findings)

            except asyncio.TimeoutError:
                check.mark_failure()
                logger.warning("Heartbeat check '%s' timed out", check.name)
            except Exception:
                check.mark_failure()
                logger.exception("Heartbeat check '%s' failed", check.name)

        self._last_tick = now

        if all_findings:
            await self._triage(all_findings)

            # Emit event for audit trail
            if self._bus:
                await self._bus.emit(Event(
                    type="heartbeat_tick",
                    agent_id=self._settings.agent_id,
                    data={
                        "findings_count": len(all_findings),
                        "checks_run": len(due_checks),
                        "tokens_used_today": self._tokens_used_today,
                    },
                ))

        return all_findings

    # ------------------------------------------------------------------
    # Triage
    # ------------------------------------------------------------------

    async def _triage(self, findings: list[Finding]) -> None:
        """Sort findings by urgency and dispatch appropriately."""
        # Sort: high first, then normal, then low
        urgency_order = {"high": 0, "normal": 1, "low": 2}
        findings.sort(key=lambda f: urgency_order.get(f.urgency, 1))

        high_findings = [f for f in findings if f.urgency == "high"]

        # High urgency: immediate Telegram notification
        if high_findings:
            lines = ["[Heartbeat] Urgent findings:"]
            for f in high_findings:
                lines.append(f"- [{f.source}] {f.summary}")
            await self._send_telegram("\n".join(lines))

        # Normal+ findings: cognitive triage if budget allows
        actionable = [f for f in findings if f.needs_action]
        if actionable and self._has_budget():
            await self._cognitive_triage(actionable)

    async def _cognitive_triage(self, findings: list[Finding]) -> HeartbeatResult:
        """Open a cognitive session to process findings."""
        result = HeartbeatResult()

        # Build a message summarizing findings
        lines = ["[Heartbeat] The following items need attention:"]
        for f in findings:
            lines.append(f"- [{f.source}] {f.summary}")
        lines.append("\nPlease review these findings and take any needed actions.")
        message = "\n".join(lines)

        session_id = f"heartbeat-{uuid4().hex[:8]}"

        try:
            response_text, _context, usage = await self._runner.run_turn(
                session_id, message,
                platform="heartbeat",
                skip_episode=True,
                is_subtask=True,
            )
            result.response = response_text or ""
            result.tokens_used = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)
            self._tokens_used_today += result.tokens_used

            logger.info(
                "Heartbeat cognitive triage used %d tokens (daily: %d/%d)",
                result.tokens_used, self._tokens_used_today,
                self._settings.heartbeat_daily_token_budget,
            )
        except Exception:
            logger.exception("Heartbeat cognitive triage failed")

        # End the session
        try:
            await self._runner.end_conversation(session_id)
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # Telegram notifications
    # ------------------------------------------------------------------

    async def _send_telegram(self, text: str) -> None:
        """Send Telegram notification if configured (direct httpx POST)."""
        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
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
            logger.warning("Heartbeat Telegram notification failed")

    # ------------------------------------------------------------------
    # Budget + quiet hours
    # ------------------------------------------------------------------

    def _in_quiet_hours(self) -> bool:
        """Check if current hour falls in quiet range."""
        hour = datetime.now(UTC).hour
        start = self._settings.heartbeat_quiet_start
        end = self._settings.heartbeat_quiet_end

        if start <= end:
            # Simple range: e.g. 9-17
            return start <= hour < end
        else:
            # Wraps midnight: e.g. 23-8
            return hour >= start or hour < end

    def _has_budget(self) -> bool:
        """Check if daily token budget is not exhausted."""
        return self._tokens_used_today < self._settings.heartbeat_daily_token_budget

    def _maybe_reset_budget(self) -> None:
        """Reset daily budget on date change."""
        today = date.today()
        if today != self._budget_date:
            self._tokens_used_today = 0
            self._budget_date = today
            logger.debug("Heartbeat daily token budget reset")

    # ------------------------------------------------------------------
    # Missed check detection
    # ------------------------------------------------------------------

    async def _detect_missed_checks(self) -> None:
        """Detect if heartbeat was down and log it."""
        if self._bus is None:
            return

        try:
            # Query last heartbeat event from DB
            from sqlalchemy import select
            from nous.storage.models import Event as EventModel

            async with self._heart.db.session() as session:
                result = await session.execute(
                    select(EventModel.created_at)
                    .where(EventModel.agent_id == self._settings.agent_id)
                    .where(EventModel.event_type == "heartbeat_tick")
                    .order_by(EventModel.created_at.desc())
                    .limit(1)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    gap = (datetime.now(UTC) - row).total_seconds()
                    if gap > self._settings.heartbeat_tick_interval * 10:
                        logger.warning(
                            "Heartbeat was offline for %.0f seconds (last tick: %s)",
                            gap, row.isoformat(),
                        )
        except Exception:
            logger.debug("Could not detect missed heartbeat checks (non-fatal)")

    # ------------------------------------------------------------------
    # Public API (for REST endpoints)
    # ------------------------------------------------------------------

    @property
    def registry(self) -> CheckRegistry:
        return self._registry

    @property
    def tokens_used_today(self) -> int:
        return self._tokens_used_today

    @property
    def last_tick(self) -> datetime | None:
        return self._last_tick

    async def trigger_tick(self) -> list[Finding]:
        """Force an immediate tick (for REST endpoint)."""
        self._maybe_reset_budget()
        return await self._tick()

    async def trigger_check(self, name: str) -> CheckResult | None:
        """Force a specific check to run (for REST endpoint)."""
        check = self._registry.get_check(name)
        if check is None:
            return None
        try:
            result = await asyncio.wait_for(check.run(), timeout=check.timeout)
            check.mark_success()
            return result
        except Exception:
            check.mark_failure()
            raise
