-- 065: R3.1 bidirectional entity indexing (F085).
-- Join table: every participating entity of a keyed fact is a retrieval key.
-- DDL only - data movement (re-normalize + seed + LLM value-side) lives in
-- scripts/backfill_r3_entity_keys.py. Reads MUST join heart.facts on
-- active = true (entity rows are not cleaned on supersession).
CREATE TABLE IF NOT EXISTS heart.fact_entity_keys (
    fact_id UUID NOT NULL REFERENCES heart.facts(id) ON DELETE CASCADE,
    entity_key VARCHAR(200) NOT NULL,
    agent_id VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fact_id, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_fact_entity_keys_agent_key
    ON heart.fact_entity_keys (agent_id, entity_key);

-- R3.2 backfill watermark: statement-level resume marker for the value-side
-- extraction backfill; also stamped by the live write path.
ALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS entity_keys_extracted_at TIMESTAMPTZ;
