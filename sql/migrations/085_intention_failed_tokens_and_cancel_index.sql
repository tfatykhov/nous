-- Migration 085: failed-attempt tokens and the cancelled-roots index (F099 Phase 2e)
--
-- brain.intentions.failed_tokens: the tokens (in plus out) that the failed
-- attempts of a claim spent. A failed attempt that is retried writes no arrival
-- row, so without this column its tokens were counted nowhere, and a lineage
-- whose turns keep failing could never reach its token budget. It is written on
-- the deepest intention of the claim only, so a root's budget is a plain sum
-- over its lineage. brain.intention_arrivals needs no change: a failed attempt
-- is not a turn, and the arrival rows keep their meaning.
--
-- idx_intentions_cancelled: the in-process view of cancelled roots is loaded at
-- startup and refreshed at every sweep from the roots that carry a cancel marker.

ALTER TABLE brain.intentions
    ADD COLUMN IF NOT EXISTS failed_tokens INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_intentions_cancelled
    ON brain.intentions (agent_id, root_cancelled_at)
    WHERE root_cancelled_at IS NOT NULL;
