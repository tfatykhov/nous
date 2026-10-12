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
    # the SAME snapshot, as (explainer, sign). sign * explainer is added
    # before testing for anomaly, so an accounted-for change residualizes to
    # ~0 and stays quiet while an UNACCOUNTED change of the same size still
    # fires at full strength.
    #
    # inactive_fact_delta rises by exactly as much as fact_count_delta falls
    # for a deactivation (and the reverse for a reactivation), hence +1. It is
    # the SIGNED net, deliberately not facts_pruned: the gross prune count
    # would over-explain any interval that also reactivated facts.
    #
    # sleep_prune_count is the part of facts_pruned a sleep cycle did, hence
    # -1: routine sleep cleanup is not drift (#720). It is a subset of the
    # same newly-inactive id set, so the residual cannot go below 0.
    #
    # An explainer that is absent (None) is unknown, not 0. When the current
    # snapshot lacks it the metric is judged raw; a baseline row lacking it
    # is left out of a residualized baseline.
    RESIDUALIZE: dict[str, tuple[str, int]] = {
        "fact_count_delta": ("inactive_fact_delta", 1),
        "facts_pruned": ("sleep_prune_count", -1),
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
        # #719 raised this floor to 100 because routine sleep-cycle cleanup
        # (stale_scan + cluster_consolidation, 78/91/83 facts on Oct 2/8/9)
        # fired at 50. facts_pruned is now residualized against
        # sleep_prune_count (#720), so that cleanup explains itself and the
        # floor is back to 50 for UNEXPLAINED prunes. With the consolidation
        # audit off, sleep_prune_count is unknown and facts_pruned is judged
        # gross, which cannot tell that cleanup from drift, so the gross path
        # keeps the #719 floor (min_abs_deviation_unresidualized).
        "facts_pruned":            {"k": 2.0, "min_samples": 10, "min_abs_deviation": 50.0,
                                    "min_abs_deviation_unresidualized": 100.0},
        # Residualization explains sleep prunes away from facts_pruned by
        # design, so a runaway sleep phase (a stale_scan burst of 2,000)
        # would be silent there. Tracked on its own, at the #719 floor that
        # routine cleanup (~80 per cycle) stays under. Null (audit off) is
        # unknown: skipped, never read as 0.
        "sleep_prune_count":       {"k": 2.0, "min_samples": 10, "min_abs_deviation": 100.0},
        "findings_created":        {"k": 2.0, "min_samples": 10},
        "episodes_compacted":      {"k": 2.0, "min_samples": 10},
        "contradictions_resolved": {"k": 2.0, "min_samples": 10},
    }

    def detect(self, current: BehaviorSnapshot, history: list[BehaviorSnapshot]) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        current_metrics = current.to_metrics_dict()
        for metric, config in self.THRESHOLDS.items():
            explainer, sign = self.RESIDUALIZE.get(metric, (None, 0))
            floor = config.get("min_abs_deviation", 0.0)
            if explainer is not None and current_metrics.get(explainer) is None:
                explainer = None  # unknown explanation: judge the raw metric
                floor = config.get("min_abs_deviation_unresidualized", floor)

            def _value_of(
                metrics: dict[str, Any], _m: str = metric, _e: str | None = explainer, _s: int = sign
            ) -> float | None:
                raw = metrics.get(_m, 0)
                if raw is None:
                    return None
                value = float(raw)
                if _e:
                    explained = metrics.get(_e)
                    if explained is None:
                        return None
                    value += _s * float(explained)
                return value

            # Per-metric version filter: a baseline row written under an
            # older definition of THIS metric (or of its explainer) is left
            # out, while the same row still counts for unchanged metrics.
            values = [
                value
                for s in history
                if metric_comparable(metric, s.metrics_version)
                and (explainer is None or metric_comparable(explainer, s.metrics_version))
                and (value := _value_of(s.to_metrics_dict())) is not None
            ]
            if len(values) < config["min_samples"]:
                continue
            mean = statistics.mean(values)
            try:
                stddev = statistics.stdev(values)
            except statistics.StatisticsError:
                continue
            current_val = _value_of(current_metrics)
            if current_val is None:
                continue  # unknown this tick: nothing to judge
            deviation = current_val - mean
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
