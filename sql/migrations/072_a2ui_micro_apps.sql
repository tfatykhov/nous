-- 072: F092.1 ephemeral micro-apps — app_spec on a2ui_surfaces
--
-- Micro-apps (kind = 'micro_app') carry a server-authoritative spec the
-- renderer never sees in full: the enumerated refine options that
-- app.refine validates submitted ids against, the data_sources that
-- app.refresh re-runs, provenance markers for model-supplied subtrees,
-- and compose metadata (intent, archetype, composed_at). It lives on the
-- surface row — not in the data model — for the same reason
-- allowed_actions does: the client is never the authority on what a
-- surface offers.
--
-- Nullable by design: template surfaces (approval_gate, decision_sweep,
-- ...) carry NULL and are unaffected.
--
-- NOTE for editors: the startup migrator splits on top-level semicolons after
-- stripping FULL-LINE comments only. Never put a comment at the end of a DDL
-- line and never put a semicolon inside a comment.

ALTER TABLE nous_system.a2ui_surfaces
    ADD COLUMN IF NOT EXISTS app_spec JSONB;
