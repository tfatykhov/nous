"""F035.3: Drift detection using z-score analysis."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from nous.observability.snapshots import BehaviorSnapshot


@dataclass
class Anomaly:
    metric: str
    current: float
    mean: float
    stddev: float
    z_score: float
    direction: str   # "up" or "down"
    severity: str    # "warning" or "alert"


class DriftDetector:
    """Z-score based behavioral drift detection."""

    THRESHOLDS: dict[str, dict[str, Any]] = {
        "fact_count_delta":        {"k": 2.0, "min_samples": 10},
        "admission_rate":          {"k": 2.0, "min_samples": 10},
        "active_censor_count":     {"k": 2.5, "min_samples": 10},
        "active_censor_delta":     {"k": 2.5, "min_samples": 10},
        "handler_error_rate":      {"k": 1.5, "min_samples": 5},
        "handler_error_count":     {"k": 1.5, "min_samples": 5},
        "events_dropped":          {"k": 1.5, "min_samples": 5},
        "facts_pruned":            {"k": 2.0, "min_samples": 10},
        "findings_created":        {"k": 2.0, "min_samples": 10},
        "episodes_compacted":      {"k": 2.0, "min_samples": 10},
        "contradictions_resolved": {"k": 2.0, "min_samples": 10},
    }

    def detect(self, current: BehaviorSnapshot, history: list[BehaviorSnapshot]) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        current_metrics = current.to_metrics_dict()
        for metric, config in self.THRESHOLDS.items():
            values = [float(s.to_metrics_dict().get(metric, 0)) for s in history]
            if len(values) < config["min_samples"]:
                continue
            mean = statistics.mean(values)
            try:
                stddev = statistics.stdev(values)
            except statistics.StatisticsError:
                continue
            if stddev == 0:
                continue
            current_val = float(current_metrics.get(metric, 0))
            z_score = (current_val - mean) / stddev
            if abs(z_score) > config["k"]:
                severity = "alert" if abs(z_score) >= 3.0 else "warning"
                anomalies.append(Anomaly(
                    metric=metric, current=current_val, mean=round(mean, 2),
                    stddev=round(stddev, 2), z_score=round(z_score, 2),
                    direction="up" if z_score > 0 else "down", severity=severity,
                ))
        return anomalies
