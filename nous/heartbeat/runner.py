"""Heartbeat runner — background tick loop with triage (F034 + F034.1).

Follows the TaskScheduler start/stop pattern: creates an asyncio.Task
that runs a periodic loop, checking due checks and triaging findings.

F034.1 adds FindingStore integration for dedup, escalation, daily digest,
and outcome tracking.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx

from nous.api.anthropic_client import AnthropicClient
from nous.api.runner import AgentRunner
from nous.brain import Brain
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart import Heart
from nous.heartbeat.finding_store import FindingStore
from nous.heartbeat.registry import CheckRegistry
from nous.heartbeat.schemas import CheckResult, Finding, FindingAction, HeartbeatResult
from nous.heartbeat.tuner import HeartbeatTuner

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
        finding_store: FindingStore | None = None,
        api_client: AnthropicClient | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._brain = brain
        self._heart = heart
        self._bus = bus
        self._http = http_client
        self._finding_store = finding_store
        self._api_client = api_client
        self._dedicated_runner: AgentRunner | None = None

        self._task: asyncio.Task | None = None
        self._running = False
        self._tick_count: int = 0
        self._tokens_used_today: int = 0
        self._budget_date: date = date.today()
        self._last_tick: datetime | None = None
        self._last_digest_date: date | None = None
        self._last_prune: datetime | None = None
        self._tuner: HeartbeatTuner = HeartbeatTuner()
        self._last_tune: datetime | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the heartbeat loop."""
        self._running = True

        # Create dedicated runner with isolated API client for triage
        if self._api_client is not None:
            self._dedicated_runner = self._runner.fork(self._api_client)
            logger.info("F034: Heartbeat using dedicated API client (isolated connection pool)")

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

        # Clean up dedicated runner and its API client
        if self._dedicated_runner is not None:
            try:
                await self._dedicated_runner.close()
            except Exception:
                logger.warning("F034: Error closing dedicated runner", exc_info=True)
            finally:
                self._dedicated_runner = None
        if self._api_client is not None:
            try:
                await self._api_client.close()
            except Exception:
                logger.warning("F034: Error closing heartbeat API client", exc_info=True)
            finally:
                self._api_client = None

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

                # F034.1: Daily digest at UTC hour 9
                await self._maybe_send_digest()

                # F034.1: Periodic prune + sweep (every 24h)
                await self._maybe_prune_and_sweep()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Heartbeat tick failed")

    async def _tick(self, urgent_only: bool = False) -> list[Finding]:
        """Run due checks and triage findings."""
        self._tick_count += 1
        now = datetime.now(UTC)
        due_checks = self._registry.get_due_checks(now)

        if urgent_only:
            due_checks = [c for c in due_checks if c.urgent_override]

        if not due_checks:
            return []

        logger.info(
            "Heartbeat tick: running %d check(s) — %s",
            len(due_checks),
            ", ".join(c.name for c in due_checks),
        )

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
            logger.info(
                "Heartbeat found %d finding(s): %s",
                len(all_findings),
                "; ".join(f"[{f.urgency}] {f.summary[:60]}" for f in all_findings),
            )
            try:
                await self._triage(all_findings)
            except Exception:
                logger.exception("Heartbeat triage crashed")

            # Emit event for audit trail
            if self._bus:
                tick_event = Event(
                    type="heartbeat_tick",
                    agent_id=self._settings.agent_id,
                    data={
                        "findings_count": len(all_findings),
                        "checks_run": len(due_checks),
                        "tokens_used_today": self._tokens_used_today,
                        "findings": [
                            {
                                "source": f.source,
                                "summary": f.summary,
                                "urgency": f.urgency,
                                "check_name": f.check_name,
                            }
                            for f in all_findings
                        ],
                        "by_source": dict(Counter(f.source for f in all_findings)),
                        "by_urgency": dict(Counter(f.urgency for f in all_findings)),
                    },
                )
                tick_event.trace_id = tick_event.event_id  # Root event
                self._current_tick_event = tick_event
                await self._bus.emit(tick_event)

        return all_findings

    # ------------------------------------------------------------------
    # Triage
    # ------------------------------------------------------------------

    async def _triage(self, findings: list[Finding]) -> None:
        """Sort findings by urgency and dispatch appropriately.

        F034.1: When FindingStore is present, each finding is routed through
        the store's state machine first. SUPPRESS -> skip, ESCALATE -> upgrade
        urgency to high, TRIAGE -> proceed normally.
        """
        # F034.1: Route through FindingStore if available
        if self._finding_store is not None:
            routed_findings: list[Finding] = []
            time_escalated_checks: set[str] = set()

            for f in findings:
                action = self._finding_store.ingest(f)
                fp = f.fingerprint()

                if action == FindingAction.SUPPRESS:
                    logger.debug("F034.1: Suppressed finding %s: %s", fp, f.summary[:60])
                    continue
                elif action == FindingAction.ESCALATE:
                    logger.info("F034.1: Escalating finding %s: %s", fp, f.summary[:60])
                    f.urgency = "high"
                    time_escalated_checks.add(f.check_name)
                    routed_findings.append(f)
                else:  # TRIAGE
                    routed_findings.append(f)

                # Acknowledge after routing to triage/escalate
                self._finding_store.acknowledge(fp)

            # F034.1: Accumulation escalation per check_name
            # (mutually exclusive with time-based escalation within a tick)
            check_names_seen = {f.check_name for f in findings}
            for check_name in check_names_seen:
                if check_name in time_escalated_checks:
                    continue  # already time-escalated, skip accumulation
                if self._finding_store.check_accumulation_escalation(check_name):
                    logger.info(
                        "F034.1: Accumulation escalation for check '%s'", check_name,
                    )
                    # Send accumulation alert via Telegram
                    ack_items = [
                        t for t in self._finding_store.get_digest_items()
                        if t.finding.check_name == check_name
                    ]
                    if ack_items:
                        lines = [f"[Heartbeat] Accumulation alert: {check_name} ({len(ack_items)} findings)"]
                        for item in ack_items[:10]:  # cap at 10 in message
                            lines.append(f"- {item.finding.summary[:80]}")
                        await self._send_telegram("\n".join(lines))

            findings = routed_findings

        if not findings:
            logger.debug("Heartbeat triage: all findings suppressed by FindingStore")
            return

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
        logger.info(
            "Heartbeat triage: %d routed, %d actionable, budget=%s",
            len(findings), len(actionable), "ok" if self._has_budget() else "exhausted",
        )
        if actionable and self._has_budget():
            await self._cognitive_triage(actionable)
        elif actionable and not self._has_budget():
            logger.warning(
                "Heartbeat budget exhausted (%d/%d tokens) — %d actionable finding(s) not triaged",
                self._tokens_used_today, self._settings.heartbeat_daily_token_budget,
                len(actionable),
            )

    def _get_triage_runner(self) -> AgentRunner:
        """Return the runner to use for cognitive triage.

        If a dedicated api_client was provided and the dedicated runner was
        initialized in start(), returns it. Otherwise falls back to the
        shared runner with a warning if api_client was set but start()
        didn't complete.
        """
        if self._api_client is not None:
            if self._dedicated_runner is not None:
                return self._dedicated_runner
            logger.warning(
                "F034: api_client provided but dedicated runner not initialized "
                "(was start() called?); falling back to shared runner"
            )
        return self._runner

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
        triage_runner = self._get_triage_runner()

        try:
            response_text, _context, usage = await triage_runner.run_turn(
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

            if self._bus:
                _parent = getattr(self, "_current_tick_event", None)
                await self._bus.emit(Event(
                    type="heartbeat_triage",
                    agent_id=self._settings.agent_id,
                    data={
                        "session_id": session_id,
                        "findings_count": len(findings),
                        "tokens_used": result.tokens_used,
                        "response_summary": result.response[:200],
                    },
                    trace_id=_parent.trace_id if _parent else None,
                    caused_by=_parent.event_id if _parent else None,
                ))
        except Exception:
            logger.exception("Heartbeat cognitive triage failed")

        # End the session
        try:
            await triage_runner.end_conversation(session_id)
        except Exception:
            pass

        return result

    # ------------------------------------------------------------------
    # F034.1: Daily digest + maintenance
    # ------------------------------------------------------------------

    async def _maybe_send_digest(self) -> None:
        """Send daily digest at UTC hour 9 if FindingStore has acknowledged items."""
        if self._finding_store is None:
            return

        now = datetime.now(UTC)
        today = now.date()

        if now.hour == 9 and self._last_digest_date != today:
            self._last_digest_date = today
            await self._daily_digest()

    async def _daily_digest(self) -> None:
        """Collect acknowledged findings and send grouped Telegram digest."""
        if self._finding_store is None:
            return

        items = self._finding_store.get_digest_items()
        if not items:
            return

        # Group by check_name
        by_check: dict[str, list] = {}
        for item in items:
            by_check.setdefault(item.finding.check_name, []).append(item)

        lines = [f"[Heartbeat] Daily digest ({len(items)} tracked findings):"]
        for check_name, check_items in sorted(by_check.items()):
            lines.append(f"\n{check_name} ({len(check_items)}):")
            for item in check_items[:5]:  # cap per check
                # Mark items near escalation with arrow
                near_escalation = ""
                if item.first_seen is not None:
                    age_h = (datetime.now(UTC) - item.first_seen).total_seconds() / 3600
                    urgency = item.finding.urgency
                    threshold = {"low": 72, "normal": 24, "high": 12}.get(urgency, 24)
                    if age_h >= threshold * 0.75:
                        near_escalation = " \u2b06\ufe0f"
                lines.append(
                    f"  - [{item.finding.urgency}] {item.finding.summary[:60]}"
                    f" (x{item.seen_count}){near_escalation}"
                )
            if len(check_items) > 5:
                lines.append(f"  ... and {len(check_items) - 5} more")

        await self._send_telegram("\n".join(lines))
        logger.info("F034.1: Sent daily digest with %d findings", len(items))

    async def _maybe_prune_and_sweep(self) -> None:
        """Run prune + sweep every 24 hours."""
        if self._finding_store is None:
            return

        now = datetime.now(UTC)
        if self._last_prune is not None and (now - self._last_prune).total_seconds() < 86400:
            return

        self._last_prune = now
        pruned = self._finding_store.prune()
        swept = self._finding_store.sweep_weak_negatives()
        if pruned or swept:
            logger.info("F034.1: Pruned %d resolved, swept %d weak_negative findings", pruned, swept)

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
    def is_running(self) -> bool:
        return self._running

    @property
    def is_quiet(self) -> bool:
        return self._in_quiet_hours()

    @property
    def registry(self) -> CheckRegistry:
        return self._registry

    @property
    def finding_store(self) -> FindingStore | None:
        return self._finding_store

    @property
    def tuner(self) -> HeartbeatTuner:
        return self._tuner

    @property
    def tokens_used_today(self) -> int:
        return self._tokens_used_today

    @property
    def last_tick(self) -> datetime | None:
        return self._last_tick

    def get_stats(self) -> dict:
        """F035.1: Return heartbeat runner statistics."""
        return {
            "total_ticks": self._tick_count,
            "last_tick_at": self._last_tick.isoformat() if self._last_tick else None,
            "currently_running": self._running,
            "tokens_used_today": self._tokens_used_today,
            "budget_remaining": max(0, self._settings.heartbeat_daily_token_budget - self._tokens_used_today),
        }

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
