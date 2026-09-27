"""F035.3: Behavioral metric snapshots for drift detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Schema version stamped into every stored behavior-snapshot metrics blob.
#: Bump whenever a metric's DEFINITION changes (scope, units, or which inputs
#: feed it) so consumers can refuse to compare across the change.
#:   1 -> original: global corpus counts, facts_pruned never populated.
#:   2 -> agent-scoped corpus counts, facts_pruned populated from the inactive
#:        count delta (so fact_count_delta residualization is meaningful).
SNAPSHOT_METRICS_VERSION = 2

#: Metrics whose DEFINITION changed at each version, including metrics that
#: were introduced then (a v1 row has no value for those, and reading one back
#: would fabricate a 0). Comparability is decided PER METRIC, not per row: a v1
#: handler_error_rate means exactly what a v2 one does, so dropping whole v1
#: rows would needlessly truncate every unchanged metric's history.
METRIC_DEFINITION_CHANGES: dict[int, frozenset[str]] = {
    2: frozenset({
        # Corpus counts became agent-scoped (v1 counted every agent's rows).
        "fact_count", "fact_count_delta",
        "episode_count", "episode_count_delta",
        "active_censor_count", "active_censor_delta",
        "procedure_count",
        # Newly populated / newly introduced (v1 stored 0 or nothing).
        "facts_pruned", "inactive_fact_count",
    }),
}


def stored_metrics_version(metrics: dict[str, Any]) -> int:
    """Version a stored metrics blob was written under. Rows predating the
    stamp carry no key and are version 1."""
    return int(metrics.get("metrics_version", 1))


def metric_comparable(metric: str, version: int) -> bool:
    """Whether ``metric`` from a row written at ``version`` is comparable with
    the same metric under the CURRENT definition.

    Every reader that aggregates a metric across stored snapshots must apply
    this per metric, or it will average incompatible definitions -- v1 fact
    counts are global while v2 are agent-scoped, so a mixed window silently
    blends another agent's corpus into this one's mean and stddev.

    A row from a NEWER writer is never comparable: this process cannot know
    what that version changed (rolling upgrade / rollback sharing one DB).
    """
    if version > SNAPSHOT_METRICS_VERSION:
        return False
    return not any(
        metric in METRIC_DEFINITION_CHANGES.get(v, frozenset())
        for v in range(version + 1, SNAPSHOT_METRICS_VERSION + 1)
    )


@dataclass
class BehaviorSnapshot:
    """Point-in-time snapshot of key system metrics."""
    timestamp: datetime
    #: Version this snapshot's metrics were written under. Not a metric (kept
    #: out of to_metrics_dict); DriftDetector uses it to skip, per metric,
    #: baseline values whose definition has since changed.
    metrics_version: int = SNAPSHOT_METRICS_VERSION

    # Memory metrics
    fact_count: int = 0
    fact_count_delta: int = 0
    # Count of soft-deactivated facts (active = false). Read in the SAME
    # query/snapshot as fact_count so facts_pruned can be derived by
    # differencing it instead of by a wall-clock window (see
    # BehaviorDriftCheck._capture_snapshot).
    inactive_fact_count: int = 0
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
            "fact_count": self.fact_count, "fact_count_delta": self.fact_count_delta,
            "inactive_fact_count": self.inactive_fact_count,
            "episode_count": self.episode_count, "episode_count_delta": self.episode_count_delta,
            "active_censor_count": self.active_censor_count, "active_censor_delta": self.active_censor_delta,
            "procedure_count": self.procedure_count, "decision_count": self.decision_count,
            "facts_admitted": self.facts_admitted, "facts_rejected_dedup": self.facts_rejected_dedup,
            "facts_rejected_admission": self.facts_rejected_admission, "admission_rate": self.admission_rate,
            "checks_run": self.checks_run, "findings_created": self.findings_created,
            "findings_resolved": self.findings_resolved, "triage_sessions_opened": self.triage_sessions_opened,
            "sleep_ran": int(self.sleep_ran), "episodes_compacted": self.episodes_compacted,
            "facts_pruned": self.facts_pruned, "contradictions_resolved": self.contradictions_resolved,
            "events_processed": self.events_processed, "events_dropped": self.events_dropped,
            "handler_error_count": self.handler_error_count, "handler_error_rate": self.handler_error_rate,
            "turns_processed": self.turns_processed, "avg_turn_latency_ms": self.avg_turn_latency_ms,
            "tool_calls": self.tool_calls,
        }
