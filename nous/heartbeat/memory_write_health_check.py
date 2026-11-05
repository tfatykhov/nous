"""The hourly memory write-health check (precision plan P4).

Runs ``nous.observability.memory_write_health.run_once`` and turns each tripped rule into a finding. Owner's rules:
the findings are for the dashboard only (``chat_delivery = False``: no Telegram at any urgency) and never hand the
problem to the agent (``needs_action=False``; the September 2026 hard deletes were made by a triage turn). It runs in
quiet hours too (``urgent_override``) so the dashboard is current. A failed run raises: the runner counts a failure,
writes no row and auto-resolves nothing.
"""

from __future__ import annotations

from typing import Any

from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding
from nous.observability import memory_write_health as mwh


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
    timeout = 30
    urgent_override = True
    chat_delivery = False

    def __init__(self, database: Any, agent_id: str) -> None:
        super().__init__()
        self._db = database
        self._agent_id = agent_id

    async def run(self) -> CheckResult:
        states = await mwh.run_once(self._db, self._agent_id)
        findings = findings_for(states)
        return CheckResult(has_updates=bool(findings), findings=findings)
