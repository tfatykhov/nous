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
    #: None when the baseline had zero variance, i.e. the deviation is
    #: unbounded in sigma. Consumers must render it as such, not as 0.
    z_score: float | None
    direction: str   # "up" or "down"
    severity: str    # "warning" or "alert"
    # Set when this metric was residualized (see DriftDetector.RESIDUALIZE).
    # current/mean/stddev are then in RESIDUAL space, not raw space, so
    # consumers must say so rather than printing the number as the raw metric.
    residualized_by: str | None = None
    raw_current: float | None = None


class DriftDetector:
    """Z-score based behavioral drift detection."""

    # Metrics whose movement is mechanically explained by another metric in
    # the SAME snapshot. The explanation is added back before testing for
    # anomaly, so an accounted-for change residualizes to ~0 and stays quiet
    # while an UNACCOUNTED change of the same size still fires at full
    # strength. facts_pruned is a positive count and fact_count_delta is
    # negative for the same event, hence addition.
    RESIDUALIZE: dict[str, str] = {
        "fact_count_delta": "facts_pruned",
    }

    # Per-metric absolute floor on |current - mean|. A z-score computed over a
    # near-constant series has a tiny denominator, so a trivially small change
    # can score many sigma. The floor suppresses those statistically-real but
    # operationally-meaningless alerts. Unset = 0.0 = no floor, which is
    # required for rate metrics in [0, 1] such as admission_rate.
    THRESHOLDS: dict[str, dict[str, Any]] = {
        # 50 facts is the materiality threshold for an unexplained swing;
        # see the tuning table in the PR that introduced residualization.
        "fact_count_delta":        {"k": 2.0, "min_samples": 10, "min_abs_deviation": 50.0},
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
            explainer = self.RESIDUALIZE.get(metric)

            def _value_of(metrics: dict[str, Any], _m: str = metric, _e: str | None = explainer) -> float:
                value = float(metrics.get(_m, 0))
                if _e:
                    value += float(metrics.get(_e, 0))
                return value

            values = [_value_of(s.to_metrics_dict()) for s in history]
            if len(values) < config["min_samples"]:
                continue
            mean = statistics.mean(values)
            try:
                stddev = statistics.stdev(values)
            except statistics.StatisticsError:
                continue
            current_val = _value_of(current_metrics)
            deviation = current_val - mean
            floor = config.get("min_abs_deviation", 0.0)
            if abs(deviation) < floor:
                continue

            if stddev == 0:
                # A residualized series is frequently constant -- usually all
                # zeros, because every change WAS explained. Skipping on zero
                # variance would then let residualization silence the very
                # metric it exists to sharpen: with a flat baseline, an
                # unexplained drop of -100 would never fire.
                #
                # The z-score is undefined here, not small. Fall back to the
                # materiality floor, which is an absolute magnitude and needs
                # no variance, and report z_score=None so consumers do not
                # print a fabricated sigma. Metrics without a floor have no
                # scale-free way to judge a departure from a constant series,
                # so they still skip.
                if floor <= 0:
                    continue
                anomalies.append(Anomaly(
                    metric=metric, current=current_val, mean=round(mean, 2),
                    stddev=0.0, z_score=None,
                    direction="up" if deviation > 0 else "down",
                    severity="alert",
                    residualized_by=explainer,
                    raw_current=float(current_metrics.get(metric, 0)) if explainer else None,
                ))
                continue

            z_score = deviation / stddev
            if abs(z_score) > config["k"]:
                severity = "alert" if abs(z_score) >= 3.0 else "warning"
                anomalies.append(Anomaly(
                    metric=metric, current=current_val, mean=round(mean, 2),
                    stddev=round(stddev, 2), z_score=round(z_score, 2),
                    direction="up" if z_score > 0 else "down", severity=severity,
                    residualized_by=explainer,
                    raw_current=float(current_metrics.get(metric, 0)) if explainer else None,
                ))
        return anomalies
