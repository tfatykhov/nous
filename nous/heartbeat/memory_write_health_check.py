"""The hourly memory write-health check (precision plan P4).

Runs ``nous.observability.memory_write_health.run_once`` and turns each tripped rule into a finding. Owner's rules:
the findings are for the dashboard only (``chat_delivery = False``: no Telegram at any urgency) and never hand the
problem to the agent (``needs_action=False``; a triage turn that judges a spike to be junk can delete rows). It runs in
quiet hours too (``urgent_override``) so the dashboard is current. A failed run raises: the runner counts a failure,
writes no row and auto-resolves nothing.

With the companion on (``set_surface_service``), it also keeps one card there in step with the tripped rules, each
with an Acknowledge button (``a2ui/builders/memory_write_health.py``): pushed under a dedup key, priority 0 and
``notify=False``, so it replaces itself in place and pings neither the phone nor Telegram; closed when nothing trips.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding
from nous.observability import memory_write_health as mwh

logger = logging.getLogger(__name__)


def findings_for(states: list[mwh.RuleState]) -> list[Finding]:
    """One finding per tripped rule state (F1 may trip once per source label)."""
    return [
        Finding(
            source=mwh.SOURCE,
            summary=s.summary,
            urgency=s.urgency,  # type: ignore[arg-type]
            needs_action=False,
            raw_data={"rule": s.id, "name": s.name, "value": s.value, "threshold": s.threshold, "detail": s.detail},
        )
        for s in states
        if s.state == "tripped"
    ]


class MemoryWriteHealthCheck(BaseCheck):
    """Daily write-health rollup, its rules and the hard-delete detector, once an hour."""

    name = "memory_write_health"
    interval = 3600
    timeout = 120  # the first run after a restart reads cold pages (the id ledger, the edges), many times a warm run
    urgent_override = True
    chat_delivery = False

    def __init__(self, database: Any, agent_id: str) -> None:
        super().__init__()
        self._db = database
        self._agent_id = agent_id
        self._surfaces: Any = None

    def set_surface_service(self, surfaces: Any) -> None:
        """Bind the companion's surface service; without one the check keeps no card."""
        self._surfaces = surfaces

    async def run(self) -> CheckResult:
        states = await mwh.run_once(self._db, self._agent_id)
        await self._sync_card(states)
        findings = findings_for(states)
        return CheckResult(has_updates=bool(findings), findings=findings)

    async def _sync_card(self, states: list[mwh.RuleState]) -> None:
        """Make the live companion card match the tripped rules: push it when none is live or the live one differs
        (a tap, an expiry or a restart can change or close it), close it when nothing trips. Compared with the live
        card, not with what this process last pushed. Never fails the check: the dashboard and the finding store
        already hold the result."""
        if self._surfaces is None:
            return
        from nous.a2ui.builders import memory_write_health as card  # only with the companion on

        try:
            live = await self._surfaces.live_data_model(card.DEDUP_KEY)
            if not card.tripped_rules(states):
                if live is not None:
                    await self._surfaces.close_by_dedup_key(card.DEDUP_KEY, status="resolved")
                return
            built = card.memory_write_health_card(states, mwh.utc_day(datetime.now(UTC)))
            if live != built.data_model:
                await self._surfaces.push_built(built, dedup_key=card.DEDUP_KEY, notify=False, telegram_ping=False)
        except Exception:
            logger.warning("Memory write health: the companion card could not be updated", exc_info=True)
