-- F058 retirement: record WHICH calibration factor produced each stored
-- confidence, and when it was applied.
--
-- Until now the factor was only implicit in the ratio confidence/confidence_raw,
-- so the calibration probe had to infer which era a row belonged to. Every
-- inference rule available is unsound:
--   * ratio ordering -- Brain._update rescales historical rows with the
--     CURRENT factor while leaving created_at alone, planting new-factor
--     ratios among old rows;
--   * created_at     -- immutable, so it misses exactly those post-deploy
--     rewrites of historical rows;
--   * updated_at     -- bumped by description-only edits that never touch
--     confidence, so it sweeps correctly-scaled old rows into the cohort.
--
-- Recording the factor directly removes the guesswork: the probe can check
-- confidence = confidence_raw * calibration_factor on any row, and check that
-- recent calibration writes used the configured factor.
--
-- Existing rows have calibration_factor IS NULL and are skipped by the probe,
-- the same fallback migration 039 used for confidence_raw. Going forward both
-- columns are populated by Brain._record and Brain._update.

ALTER TABLE brain.decisions
    ADD COLUMN IF NOT EXISTS calibration_factor double precision;

ALTER TABLE brain.decisions
    ADD COLUMN IF NOT EXISTS calibration_applied_at TIMESTAMPTZ;

-- Supports "the most recent calibration writes" lookups without scanning the
-- whole table; partial because historical rows are permanently NULL.
CREATE INDEX IF NOT EXISTS idx_decisions_calibration_applied_at
    ON brain.decisions(calibration_applied_at DESC)
    WHERE calibration_factor IS NOT NULL;

COMMENT ON COLUMN brain.decisions.calibration_factor IS
    'F058: the temperature-scaling factor actually applied to produce '
    'brain.decisions.confidence from confidence_raw. NULL for rows written '
    'before this column existed.';

COMMENT ON COLUMN brain.decisions.calibration_applied_at IS
    'F058: when calibration_factor was last applied. Distinct from updated_at, '
    'which also moves for edits that never touch confidence.';
