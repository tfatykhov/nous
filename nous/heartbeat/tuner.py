"""Self-tuning heartbeat engine (F034.3).

Adjusts check parameters based on finding outcome history.
Runs periodically and makes conservative, bounded adjustments
with automatic rollback if outcomes degrade.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from nous.heartbeat.schemas import (
    OutcomeSignal,
    TuningAdjustment,
    TuningReport,
)

logger = logging.getLogger(__name__)


class HeartbeatTuner:
    """Adjusts check parameters based on finding outcome history (F034.3).

    The tuner runs periodically (triggered by runner or REST API) and
    examines outcome signals from the FindingStore to determine whether
    each check's parameters should be relaxed (less sensitive) or
    tightened (more sensitive).

    Guardrails:
    - Parameter bounds enforced by BaseCheck.set_param()
    - Learning rate caps step size to 10% of range
    - Minimum 10 samples before any adjustment
    - Max 1 parameter adjusted per check per cycle
    - Cross-cycle rollback if negative rate increases >80%
    - Pinned parameters (manual overrides) are never adjusted
    """

    LEARNING_RATE = 0.1
    MIN_SAMPLES = 10  # default; overridable via constructor (HB-3 review follow-up)
    MAX_PARAMS_PER_CHECK = 1  # Only adjust 1 param per check per cycle

    def __init__(self, min_samples: int | None = None) -> None:
        # Cross-cycle snapshots: check_name -> {param_name: value}
        self._snapshots: dict[str, dict[str, float]] = {}
        self._last_tune: datetime | None = None
        self._last_report: TuningReport | None = None
        # Audit (HB-3 review): honor the documented
        # NOUS_HEARTBEAT_TUNING_MIN_SAMPLES setting. Previously the class
        # constant was used unconditionally, so the env var was inert.
        self.min_samples = min_samples if min_samples is not None else self.MIN_SAMPLES

    async def tune(self, finding_store: object, registry: object) -> TuningReport:
        """Run tuning pass over all checks.

        Args:
            finding_store: A FindingStore instance with get_outcomes_for_check() method.
            registry: A CheckRegistry instance with all_checks() method.

        Returns:
            TuningReport with adjustments made and checks skipped.
        """
        report = TuningReport(timestamp=datetime.now(UTC))

        for check in registry.all_checks():  # type: ignore[union-attr]
            # Check rollback from previous cycle first
            if check.name in self._snapshots:
                self._check_and_rollback(check, finding_store, report)

            outcomes = finding_store.get_outcomes_for_check(check.name)  # type: ignore[union-attr]

            if len(outcomes) < self.min_samples:
                report.skipped_checks.append(check.name)
                continue

            # Snapshot current params BEFORE adjusting
            self._snapshots[check.name] = {
                name: p.value for name, p in check.tunable_params().items()
            }

            adjustments = self._compute_adjustments(check, outcomes)

            # Apply at most MAX_PARAMS_PER_CHECK
            applied = 0
            for param_name, direction in adjustments.items():
                if applied >= self.MAX_PARAMS_PER_CHECK:
                    break
                old_val, new_val = self._apply_adjustment(check, param_name, direction)
                if old_val != new_val:
                    report.adjustments.append(TuningAdjustment(
                        check_name=check.name,
                        param_name=param_name,
                        old_value=old_val,
                        new_value=new_val,
                        direction=direction,
                        sample_count=len(outcomes),
                        positive_rate=self._positive_rate(outcomes),
                        negative_rate=self._negative_rate(outcomes),
                    ))
                    applied += 1

        self._last_tune = datetime.now(UTC)
        self._last_report = report
        return report

    def _compute_adjustments(self, check: object, outcomes: list) -> dict[str, str]:
        """Determine which parameters to relax or tighten.

        - >60% negative outcomes: relax (make less sensitive)
        - >80% positive outcomes: tighten (make more sensitive, catch more)
        - Otherwise: no change
        """
        pos_rate = self._positive_rate(outcomes)
        neg_rate = self._negative_rate(outcomes)

        adjustments: dict[str, str] = {}
        if neg_rate > 0.6:
            # Most findings are noise -- relax (make less sensitive)
            for name in check.tunable_params():  # type: ignore[union-attr]
                adjustments[name] = "relax"
        elif pos_rate > 0.8:
            # Almost all findings are useful -- could catch more
            for name in check.tunable_params():  # type: ignore[union-attr]
                adjustments[name] = "tighten"

        return adjustments

    def _apply_adjustment(
        self, check: object, param_name: str, direction: str,
    ) -> tuple[float, float]:
        """Apply a single parameter adjustment. Returns (old_value, new_value)."""
        param = check.get_param(param_name)  # type: ignore[union-attr]
        if param is None or param.pinned:
            return (0, 0)

        old_val = param.value
        param_range = param.max_val - param.min_val
        step = param.step * self.LEARNING_RATE * param_range
        # Clamp step to max 10% of range
        max_step = param_range * 0.1
        step = min(step, max_step)

        # Direction semantics: relax = fewer findings, tighten = more.
        # For sensitivity thresholds that is +step / -step; for volume
        # params (increases_findings=True: lookback windows, item caps)
        # the sign inverts — raising them produces MORE findings.
        if param.increases_findings:
            step = -step
        if direction == "relax":
            new_val = old_val + step
        elif direction == "tighten":
            new_val = old_val - step
        else:
            return (old_val, old_val)

        check.set_param(param_name, new_val)  # type: ignore[union-attr]
        actual = check.get_param(param_name)  # type: ignore[union-attr]
        return (old_val, actual.value if actual else old_val)

    def _check_and_rollback(
        self, check: object, finding_store: object, report: TuningReport,
    ) -> None:
        """Check if previous adjustment degraded outcomes. Rollback if so.

        Uses cross-cycle comparison: outcomes accumulated AFTER the previous
        adjustment are compared against the rollback threshold.
        """
        prev_snapshot = self._snapshots.get(check.name, {})  # type: ignore[union-attr]
        if not prev_snapshot:
            return

        # Get outcomes since last tune (these reflect the adjusted parameters)
        outcomes = finding_store.get_outcomes_for_check(check.name)  # type: ignore[union-attr]
        if len(outcomes) < 5:  # Need some data to evaluate
            return

        neg_rate = self._negative_rate(outcomes)
        # If negative rate > 80% after adjustment, rollback
        if neg_rate > 0.8:
            for param_name, old_value in prev_snapshot.items():
                p = check.get_param(param_name)  # type: ignore[union-attr]
                if p and not p.pinned:
                    check.set_param(param_name, old_value)  # type: ignore[union-attr]
                    report.adjustments.append(TuningAdjustment(
                        check_name=check.name,  # type: ignore[union-attr]
                        param_name=param_name,
                        old_value=p.value,
                        new_value=old_value,
                        direction="rollback",
                        sample_count=len(outcomes),
                        positive_rate=self._positive_rate(outcomes),
                        negative_rate=neg_rate,
                    ))
            del self._snapshots[check.name]  # type: ignore[union-attr]

    @staticmethod
    def _positive_rate(outcomes: list) -> float:
        """Fraction of outcomes that are positive signals."""
        if not outcomes:
            return 0
        return sum(
            1 for o in outcomes
            if getattr(o, "outcome", None) in (OutcomeSignal.STRONG_POSITIVE, OutcomeSignal.POSITIVE)
        ) / len(outcomes)

    @staticmethod
    def _negative_rate(outcomes: list) -> float:
        """Fraction of outcomes that are negative signals."""
        if not outcomes:
            return 0
        return sum(
            1 for o in outcomes
            if getattr(o, "outcome", None) in (OutcomeSignal.STRONG_NEGATIVE, OutcomeSignal.NEGATIVE)
        ) / len(outcomes)

    def generate_report_text(self, report: TuningReport) -> str:
        """Generate human-readable report for Telegram/facts."""
        if not report.adjustments and not report.skipped_checks:
            return "Heartbeat Tuning: No changes needed."

        lines = [
            f"Heartbeat Tuning Report ({report.timestamp.strftime('%Y-%m-%d') if report.timestamp else 'now'})",
            "",
        ]

        # Group adjustments by check
        by_check: dict[str, list[TuningAdjustment]] = {}
        for adj in report.adjustments:
            by_check.setdefault(adj.check_name, []).append(adj)

        for check_name, adjs in by_check.items():
            lines.append(f"{check_name}:")
            for adj in adjs:
                lines.append(
                    f"  - {adj.param_name}: {adj.old_value:.2f} -> {adj.new_value:.2f} ({adj.direction})"
                )
            lines.append("")

        if report.skipped_checks:
            lines.append(f"No changes: {', '.join(report.skipped_checks)} (insufficient data)")

        return "\n".join(lines)

    @property
    def last_report(self) -> TuningReport | None:
        """Most recent tuning report."""
        return self._last_report

    @property
    def last_tune_time(self) -> datetime | None:
        """Timestamp of most recent tuning pass."""
        return self._last_tune
