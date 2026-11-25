-- 123: nous_system.entity_profile, per-entity profiles (F119 PR 1)
--
-- One row per agent and entity key (a key of heart.fact_entity_keys). A sleep phase writes a short profile of each
-- key with enough active facts (nous/heart/entity_profile.py): one model call decides whether the key names an
-- entity and writes its lines, each citing 1 to 3 of the key's facts. Nothing reads a profile in a turn yet.
-- status: profile (lines written), not_entity (the model's verdict), owner (the keys user and assistant and the
-- owner's own keys, decided in code, no call), asked (a call made and no reply judged yet) or failed (a reply
-- that could not be used, to a key with no profile or verdict).
-- lines: [{id, text, fact_ids, said_on, date_from}]. said_on is a date taken from the cited facts, never from the
-- model, and date_from names the column it came from (mentioned_at or learned_at). Each write moves the old lines
-- to previous_lines. rejected: up to 20 line texts the owner deleted, never written again.
-- source_hash: digest of the facts the writer saw at the last call. fact_count: the key's active facts then.
-- source_count: the facts the writer saw. tokens: the rendered profile's size (characters divided by 4).
-- written_at: the last model call for the row. The daily call limit counts the rows written in the last 24 hours.
-- updated_at: the last write other than counting a call, so a call that gets no reply does not move it. The rests
-- of not_entity and failed rows are measured from it.
-- active: soft delete. A row that is not active is never written again, and no row is ever deleted.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Rollback is
-- DROP TABLE IF EXISTS nous_system.entity_profile and deleting version 123 from schema_migrations.

CREATE TABLE IF NOT EXISTS nous_system.entity_profile (
    agent_id          VARCHAR(100) NOT NULL,
    entity_key        VARCHAR(200) NOT NULL,
    status            TEXT         NOT NULL,
    display_name      TEXT,
    kind              TEXT,
    lines             JSONB        NOT NULL DEFAULT '[]',
    previous_lines    JSONB        NOT NULL DEFAULT '[]',
    rejected          JSONB        NOT NULL DEFAULT '[]',
    source_hash       TEXT         NOT NULL,
    fact_count        INT          NOT NULL DEFAULT 0,
    source_count      INT          NOT NULL DEFAULT 0,
    tokens            INT          NOT NULL DEFAULT 0,
    active            BOOLEAN      NOT NULL DEFAULT TRUE,
    written_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, entity_key)
);

CREATE INDEX IF NOT EXISTS ix_entity_profile_status ON nous_system.entity_profile (agent_id, status) WHERE active;
