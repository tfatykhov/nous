-- 071: F092 A2UI Companion — surfaces, outbox, actions
--
-- Three tables backing the A2UI push surface (spec F092, plan
-- docs/plans/2026-08-29-f092-a2ui-companion-phase1.md).
--
-- a2ui_surfaces holds the AUTHORITATIVE current state of every surface
-- (components + data_model JSONB, updated on every mutation). The outbox is
-- the delta log for live SSE clients only; snapshot hydration reads the
-- surface row, so replay is an optimization, never the source of truth.
-- That split is what makes reconnect hydration-first and cheap.
--
-- a2ui_actions is the DURABLE audit of user actions. The F032 execution
-- ledger is in-memory and session-scoped (nous/cognitive/execution_ledger.py),
-- so it cannot serve as the audit record for companion actions, which have no
-- chat session and must survive restart. ledger_entry_id is reserved.
--
-- NOTE for editors: the startup migrator splits on top-level semicolons after
-- stripping FULL-LINE comments only. Never put a comment at the end of a DDL
-- line and never put a semicolon inside a comment.

CREATE TABLE IF NOT EXISTS nous_system.a2ui_surfaces (
    surface_id       TEXT PRIMARY KEY,
    agent_id         TEXT        NOT NULL,
    origin           TEXT        NOT NULL,
    kind             TEXT        NOT NULL,
    catalog_id       TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'live',
    priority         SMALLINT    NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 2),
    title            TEXT        NOT NULL,
    components       JSONB       NOT NULL,
    data_model       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    allowed_actions  TEXT[]      NOT NULL DEFAULT '{}',
    dedup_key        TEXT,
    nonce            TEXT        NOT NULL,
    session_id       TEXT,
    trace_id         TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_a2ui_surfaces_feed
    ON nous_system.a2ui_surfaces (status, priority DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_a2ui_surfaces_expiry
    ON nous_system.a2ui_surfaces (expires_at) WHERE status = 'live';

CREATE UNIQUE INDEX IF NOT EXISTS idx_a2ui_surfaces_dedup
    ON nous_system.a2ui_surfaces (agent_id, dedup_key)
    WHERE status = 'live' AND dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS nous_system.a2ui_outbox (
    seq         BIGSERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    surface_id  TEXT NOT NULL REFERENCES nous_system.a2ui_surfaces(surface_id) ON DELETE CASCADE,
    envelope    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_a2ui_outbox_surface
    ON nous_system.a2ui_outbox (surface_id, seq);

CREATE TABLE IF NOT EXISTS nous_system.a2ui_actions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id             TEXT NOT NULL,
    surface_id           TEXT NOT NULL,
    action_name          TEXT NOT NULL,
    actor                TEXT NOT NULL DEFAULT 'unattributed',
    source_component_id  TEXT,
    context              JSONB NOT NULL DEFAULT '{}'::jsonb,
    data_model           JSONB,
    status               TEXT NOT NULL DEFAULT 'pending',
    rejection_reason     TEXT,
    ledger_entry_id      UUID,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_a2ui_actions_surface
    ON nous_system.a2ui_actions (surface_id, created_at DESC);
