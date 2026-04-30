-- F058: Add confidence_raw column to brain.decisions for calibration scaling.
--
-- Stored `confidence` will hold the calibrated value (post temperature
-- scaling) so all downstream consumers — guardrails, quality scoring,
-- supersession threshold, deliberation gates — automatically operate on
-- calibrated input. The original agent-recorded confidence is preserved
-- in `confidence_raw` so calibration evaluation (scripts/eval/eval_calibration.py)
-- can keep measuring drift over time.
--
-- Existing rows have `confidence_raw IS NULL`; the calibration eval falls
-- back to `confidence` for those, treating historical decisions as
-- pre-calibration. Going forward every new decision gets both columns
-- populated by Brain._record().

ALTER TABLE brain.decisions ADD COLUMN confidence_raw double precision;

CREATE INDEX idx_decisions_confidence_raw ON brain.decisions(confidence_raw)
    WHERE confidence_raw IS NOT NULL;

COMMENT ON COLUMN brain.decisions.confidence_raw IS
    'Agent-recorded confidence before temperature scaling; brain.decisions.confidence holds the calibrated value (F058).';
