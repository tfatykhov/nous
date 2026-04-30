"""F058: Post-hoc temperature scaling for decision confidence.

The agent's raw confidence numbers are systemically overconfident — Nous
prod data measured a +19.8% gap between mean confidence (0.834) and mean
strict accuracy (0.636) over 401 reviewed decisions, with Brier 0.252
sitting at the random-guess baseline. See `reports/calibration_eval.md`.

Mitigation is a single global scaling factor applied at decision-write
time (Brain._record). The factor is env-tunable via
`NOUS_CONFIDENCE_CALIBRATION_FACTOR`; the default 0.7627 was derived
empirically from the simulation in `scripts/eval/simulate_calibration_fix.py`
which compared none/global/per-category scaling and selected global by
ECE (0.0333 vs 0.0506 for per-category — global beats per-category here
because small-n categories like security and integration become
*underconfident* under per-category scaling).

Why write-time and not read-time:
- Single integration point (Brain._record) covers every downstream gate
  (guardrails, quality, supersession, deliberation) without per-site
  edits.
- The original agent-recorded confidence is preserved in
  brain.decisions.confidence_raw so calibration eval can keep measuring
  drift and recompute factors when the agent's overconfidence shifts.

Why not Platt/isotonic regression:
- 401 decisions is enough for a global factor but not enough to fit per-
  category sigmoids without overfitting (security n=11, integration n=5).
- Per-category match-the-mean scaling was simulated and rejected — it
  drives security from gap +0.010 to -0.208 (overcorrection).
- A global linear scale is robust at this sample size and easy to retune
  via the env var as more data accrues.
"""
from __future__ import annotations


# Default factor matches the empirical eval: agent confidence is ~31%
# higher than warranted on average, so multiply by 0.7627 to align mean
# claimed confidence with mean strict accuracy.
DEFAULT_CALIBRATION_FACTOR = 0.7627


def calibrate_confidence(raw_confidence: float, factor: float) -> float:
    """Apply temperature scaling to a raw agent-recorded confidence value.

    Args:
        raw_confidence: Agent's claimed confidence in [0.0, 1.0].
        factor: Multiplicative scale (typically 0.0-1.0). Pass 1.0 to
            disable scaling and pass through the raw value.

    Returns:
        Calibrated confidence clipped to [0.0, 1.0].

    A factor below 1.0 corrects overconfidence; above 1.0 would correct
    underconfidence. Values outside [0.0, 1.0] after scaling are clipped.
    """
    if factor == 1.0:
        return raw_confidence
    scaled = raw_confidence * factor
    if scaled < 0.0:
        return 0.0
    if scaled > 1.0:
        return 1.0
    return scaled
