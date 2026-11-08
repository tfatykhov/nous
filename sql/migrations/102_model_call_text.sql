-- 102: the model-call text, kept as blocks addressed by their hash (F108 agent work view, Phase 1, full text)
--
-- model_text_block: one row per distinct block of text an agent's model calls sent or got back (a system block, a
-- message, the tool list or a response), written by the model-call text store (NOUS_MODEL_CALL_TEXT_ENABLED, off by
-- default, and only with NOUS_MODEL_CALL_LOG_ENABLED on). body is redacted, its base64 attachments replaced by a
-- marker, and cut at NOUS_MODEL_CALL_TEXT_BLOCK_MAX_CHARS by the writer. hash is the sha256 hex of body, so a block
-- that repeats (the system prompt, the tool list, the earlier messages of a session) is stored once per agent.
-- size_chars is the length of body. created_at means last used: every store that sends the block again touches it,
-- and the text sweep deletes a block no call references once it is old.
--
-- model_call_block_ref: a call's blocks in order, part (system, messages, tools or response) then ord, deleted with
-- the call. A store inserts a call's blocks and its refs in one transaction, after the call's row is committed.
--
-- No column is named like a credential (scripts/ops/db_roles.sql section 5d).
-- Writer and reader: the app only. These are not reader tables: the db_query reader reads neither (db_roles.sql does
-- not list them), so this migration needs no step of the role script.
-- Created by the application role through the migrator: the tables and their indexes, nothing else.

CREATE TABLE IF NOT EXISTS nous_system.model_text_block (
    agent_id    TEXT         NOT NULL,
    hash        TEXT         NOT NULL,
    body        TEXT         NOT NULL,
    size_chars  INT          NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, hash)
);

CREATE TABLE IF NOT EXISTS nous_system.model_call_block_ref (
    call_id     UUID         NOT NULL REFERENCES nous_system.model_call_log (id) ON DELETE CASCADE,
    agent_id    TEXT         NOT NULL,
    part        TEXT         NOT NULL,
    ord         INT          NOT NULL,
    block_hash  TEXT         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (call_id, part, ord),
    FOREIGN KEY (agent_id, block_hash) REFERENCES nous_system.model_text_block (agent_id, hash)
);

-- The text sweep: refs older than the retention.
CREATE INDEX IF NOT EXISTS idx_model_call_block_ref_created
    ON nous_system.model_call_block_ref (created_at);

-- Whether a block is still referenced (the sweep), and the foreign key's checks when a block is deleted.
CREATE INDEX IF NOT EXISTS idx_model_call_block_ref_block
    ON nous_system.model_call_block_ref (agent_id, block_hash);
