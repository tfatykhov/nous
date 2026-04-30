-- F058: Add confidence_raw column to brain.decisions for calibration scaling.
--
-- Stored confidence holds the calibrated value (post temperature scaling)
-- so all downstream consumers (guardrails, quality, supersession threshold,
-- deliberation gates) automatically operate on calibrated input. The
-- original agent-recorded confidence is preserved in confidence_raw so
-- calibration evaluation can keep measuring drift over time.
--
-- Existing rows have confidence_raw IS NULL. The eval falls back to
-- confidence for those, treating historical decisions as pre-calibration.
-- Going forward every new decision gets both columns populated by
-- Brain._record and Brain._update.

ALTER TABLE brain.decisions ADD COLUMN IF NOT EXISTS confidence_raw double precision;

CREATE INDEX IF NOT EXISTS idx_decisions_confidence_raw ON brain.decisions(confidence_raw)
    WHERE confidence_raw IS NOT NULL;

COMMENT ON COLUMN brain.decisions.confidence_raw IS
    'F058: agent-recorded confidence before temperature scaling. brain.decisions.confidence holds the calibrated value.';
