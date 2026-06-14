-- F044 tinyHippo-Lite v1 — STC (Synaptic Tagging & Capture) state machine.
-- Telemetry-only first slice: adds the edge state columns + the LTP counter
-- the promotion gate reads. NO homeostatic downscale, NO weight-floor
-- mortality, NO replay telemetry in v1 (those are deferred).
--
-- Defaults are set at the column/constraint level so a pre-migration snapshot
-- backfills clean: every existing edge becomes a 'tagged' / ltp_count=0 row,
-- which is a no-op until the (flag-gated) reinforcement hooks start counting.
-- Feature-off behavior is therefore bit-identical to pre-F044 main.

ALTER TABLE brain.graph_edges
    ADD COLUMN IF NOT EXISTS consolidation_state TEXT NOT NULL DEFAULT 'tagged',
    ADD COLUMN IF NOT EXISTS ltp_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_ltp_at TIMESTAMPTZ NULL;

-- Guard the two-value state machine at the DB level.
ALTER TABLE brain.graph_edges
    DROP CONSTRAINT IF EXISTS graph_edges_consolidation_state_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT graph_edges_consolidation_state_check
    CHECK (consolidation_state IN ('tagged', 'consolidated'));

-- Partial index so the per-cycle promotion gate (tagged + ltp_count >= PRP)
-- and the telemetry counts scan only the tagged frontier, not the whole table.
CREATE INDEX IF NOT EXISTS idx_graph_edges_stc_tagged
    ON brain.graph_edges (agent_id, ltp_count)
    WHERE consolidation_state = 'tagged';
