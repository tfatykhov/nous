-- 092: procedure version history (F102)
--
-- Every write that changes a procedure's body (create, update_body, append_notes, a
-- create-time dedupe merge, a restore) bumps heart.procedures.version and records the
-- body it writes as one heart.procedure_versions row. The table holds every version,
-- the current one included, so restore(procedure_id, version) can bring back any of them.
-- Existing rows are backfilled as version 1 (change_source 'backfill').
--
-- idempotency_key is a create request's key (ProcedureInput.idempotency_key), kept on the
-- version that request wrote (the created row's v1, or the version a dedupe merge wrote),
-- with request_fingerprint (sha256 of the request) so a replay with the same key and a
-- different request is refused. Unique per agent.
--
-- procedure_id references heart.procedures ON DELETE CASCADE, as procedure_task_affinity
-- does: the app never hard deletes a procedure (soft deletes only), and a cleanup that
-- does (test teardown, scripts/diag/_seed_dedup_db.py) takes the row's history with it.
-- Splitter-safe: plain statements, no DO block (run_migrations wraps the file in one
-- transaction). Numbered 092: 091 is taken by F101, being written at the same time.

ALTER TABLE heart.procedures ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS heart.procedure_versions (
    id                   UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id             VARCHAR(100)  NOT NULL,
    procedure_id         UUID          NOT NULL REFERENCES heart.procedures(id) ON DELETE CASCADE,
    version              INTEGER       NOT NULL,
    name                 VARCHAR(500)  NOT NULL,
    domain               VARCHAR(100),
    description          TEXT,
    goals                TEXT[],
    core_patterns        TEXT[],
    core_tools           TEXT[],
    core_concepts        TEXT[],
    implementation_notes TEXT[],
    tags                 TEXT[],
    kind                 VARCHAR(100),
    runtime_metadata     JSONB,
    change_source        VARCHAR(50)   NOT NULL,
    reason               TEXT,
    idempotency_key      TEXT,
    request_fingerprint  VARCHAR(64),
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_procedure_versions_version UNIQUE (procedure_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_procedure_versions_idempotency_key
    ON heart.procedure_versions (agent_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

INSERT INTO heart.procedure_versions (
    agent_id, procedure_id, version, name, domain, description, goals, core_patterns,
    core_tools, core_concepts, implementation_notes, tags, kind, runtime_metadata,
    change_source, created_at
)
SELECT
    p.agent_id, p.id, p.version, p.name, p.domain, p.description, p.goals, p.core_patterns,
    p.core_tools, p.core_concepts, p.implementation_notes, p.tags, p.kind, p.runtime_metadata,
    'backfill', COALESCE(p.updated_at, p.created_at, NOW())
FROM heart.procedures p
WHERE NOT EXISTS (
    SELECT 1 FROM heart.procedure_versions v WHERE v.procedure_id = p.id AND v.version = p.version
);
