-- F050: Multi-query expansion cache.
-- Global table (no agent_id) — variants are semantic, not per-agent.
-- Keyed by SHA-256 of canonicalized query (NFKC-normalize -> lowercase -> strip).
--
-- Migration numbering: 035 and 036 are intentional gaps in the migrations
-- folder (post-mortem: F049-era was renumbered after merge conflicts left holes
-- in the sequence). 037 = F051 eval_runs. 038 = this F050 cache.
--
-- TTL sweep handler ships in F050.2 (~30 LOC). For now, operators may
-- manually `DELETE WHERE last_used_at < now() - interval '30 days'`.

CREATE TABLE IF NOT EXISTS heart.query_expansions (
    input_hash    BYTEA PRIMARY KEY,
    query_text    TEXT NOT NULL,
    variants      JSONB NOT NULL,
    model         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hit_count     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_query_expansions_last_used
    ON heart.query_expansions(last_used_at);

COMMENT ON TABLE heart.query_expansions IS
    'F050: Haiku-generated query-expansion variants, keyed by SHA-256 input hash.';
COMMENT ON COLUMN heart.query_expansions.input_hash IS
    'F050: canonical_input_hash(query) = sha256(NFKC-normalize -> lower -> strip) — 32 bytes.';
COMMENT ON COLUMN heart.query_expansions.variants IS
    'F050: JSON array of variant strings, e.g. ["original", "variant1", "variant2"].';
