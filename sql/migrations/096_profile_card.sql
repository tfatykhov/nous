-- 096: the bounded profile card
--
-- A short card of durable profile lines (preference, person, rule) that a sleep phase writes from the
-- agent's tier-1 facts (nous/heart/profile_card.py). One row per agent: each write moves the old entries
-- to previous_entries, so there is no version table.
-- entries: [{id, text, fact_ids}], every line cites 1 to 5 source fact ids.
-- source_hash: digest of the source facts (id, content, tags, active, superseded_by) at the last write.
-- An unchanged digest skips the model call.
-- rejected: up to 20 line texts the owner deleted. A later write never brings one back.
-- tokens: the rendered card's size (characters divided by 4).
-- Writer: the app (the sleep phase, POST /profile-card/rebuild and DELETE /profile-card/entries/{id}).
-- No other role reads it: the db_query reader keeps its 14 tables, and the scorer does not need it.
-- Created by the application role through the migrator: the table, nothing else.

CREATE TABLE IF NOT EXISTS nous_system.profile_card (
    agent_id          VARCHAR(100) PRIMARY KEY,
    entries           JSONB        NOT NULL DEFAULT '[]',
    previous_entries  JSONB        NOT NULL DEFAULT '[]',
    source_hash       TEXT         NOT NULL,
    rejected          JSONB        NOT NULL DEFAULT '[]',
    tokens            INT          NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
