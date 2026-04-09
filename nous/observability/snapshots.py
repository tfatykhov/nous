"""F035.3: Behavioral metric snapshots for drift detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class BehaviorSnapshot:
    """Point-in-time snapshot of key system metrics."""

    timestamp: datetime

    # Memory metrics
    fact_count: int = 0
    fact_count_delta: int = 0
    episode_count: int = 0
    episode_count_delta: int = 0
    active_censor_count: int = 0
    active_censor_delta: int = 0
    procedure_count: int = 0
    decision_count: int = 0

    # Admission metrics
    facts_admitted: int = 0
    facts_rejected_dedup: int = 0
    facts_rejected_admission: int = 0
    admission_rate: float = 0.0

    # Heartbeat metrics
    checks_run: int = 0
    findings_created: int = 0
    findings_resolved: int = 0
    triage_sessions_opened: int = 0
    interval_changes: list[dict] = field(default_factory=list)

    # Sleep metrics
    sleep_ran: bool = False
    episodes_compacted: int = 0
    facts_pruned: int = 0
    contradictions_resolved: int = 0

    # Event bus health
    events_processed: int = 0
    events_dropped: int = 0
    handler_error_count: int = 0
    handler_error_rate: float = 0.0

    # Conversation metrics
    turns_processed: int = 0
    avg_turn_latency_ms: float = 0.0
    tool_calls: int = 0

    def to_metrics_dict(self) -> dict[str, Any]:
        return {
            "fact_count": self.fact_count,
            "fact_count_delta": self.fact_count_delta,
            "episode_count": self.episode_count,
            "episode_count_delta": self.episode_count_delta,
            "active_censor_count": self.active_censor_count,
            "active_censor_delta": self.active_censor_delta,
            "procedure_count": self.procedure_count,
            "decision_count": self.decision_count,
            "facts_admitted": self.facts_admitted,
            "facts_rejected_dedup": self.facts_rejected_dedup,
            "facts_rejected_admission": self.facts_rejected_admission,
            "admission_rate": self.admission_rate,
            "checks_run": self.checks_run,
            "findings_created": self.findings_created,
            "findings_resolved": self.findings_resolved,
            "triage_sessions_opened": self.triage_sessions_opened,
            "sleep_ran": int(self.sleep_ran),
            "episodes_compacted": self.episodes_compacted,
            "facts_pruned": self.facts_pruned,
            "contradictions_resolved": self.contradictions_resolved,
            "events_processed": self.events_processed,
            "events_dropped": self.events_dropped,
            "handler_error_count": self.handler_error_count,
            "handler_error_rate": self.handler_error_rate,
            "turns_processed": self.turns_processed,
            "avg_turn_latency_ms": self.avg_turn_latency_ms,
            "tool_calls": self.tool_calls,
        }
