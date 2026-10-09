"""F035.3: Drift detection using z-score analysis."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from nous.observability.snapshots import BehaviorSnapshot, metric_comparable


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
    # strength. inactive_fact_delta rises by exactly as much as
    # fact_count_delta falls for a deactivation (and the reverse for a
    # reactivation), hence addition. It is the SIGNED net, deliberately not
    # facts_pruned: the gross prune count would over-explain any interval
    # that also reactivated facts.
    RESIDUALIZE: dict[str, str] = {
        "fact_count_delta": "inactive_fact_delta",
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
        # Same 50-fact materiality bar as fact_count_delta, and for a reason
        # that only exists because of residualization: a mass prune cancels
        # out of fact_count_delta by design, so facts_pruned is the ONLY
        # metric left that can report it. Without a floor, a baseline of ten
        # quiet (zero-prune) snapshots has no variance, the zero-variance
        # branch has no scale to judge against, and the prune is silent on
        # both metrics at once.
        #
        # Floor raised from 50 → 100 (fix/facts-pruned-drift-fp): a normal
        # sleep cycle running stale_scan + cluster_consolidation legitimately
        # prunes 70–100 ephemeral micro-artifact facts (Garmin sync states,
        # fitness snapshots, bare issue# fragments). Observed: Oct 2=78,
        # Oct 8=91, Oct 9=83 — all false positives at k=2/floor=50.
        # Raising the floor to 100 suppresses routine sleep-cycle cleanup
        # while still catching genuinely anomalous mass pruning (RL sweep
        # gone wrong, etc.) which would manifest at 150+.
        # Long-term fix: residualize facts_pruned against a sleep_prune_count
        # metric in the snapshot (analogous to inactive_fact_delta for
        # fact_count_delta). Tracked in GitHub issue.
        "facts_pruned":            {"k": 2.0, "min_samples": 10, "min_abs_deviation": 100.0},
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

            # Per-metric version filter: a baseline row written under an
            # older definition of THIS metric (or of its explainer) is left
            # out, while the same row still counts for unchanged metrics.
            values = [
                _value_of(s.to_metrics_dict())
                for s in history
                if metric_comparable(metric, s.metrics_version)
                and (explainer is None or metric_comparable(explainer, s.metrics_version))
            ]
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
