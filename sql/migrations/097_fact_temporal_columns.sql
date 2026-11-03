-- 097: event time and statement time on facts (bi-temporal facts, PR1).
-- valid_from and valid_to are event time, the days the fact was true.
-- A one-day event has valid_from = valid_to = event_date. A closed span or a
-- coarse date ("Feb 2026") has both set and event_date NULL. An open state
-- ("since Jan") has valid_to NULL. No known date leaves all three NULL.
-- mentioned_at is statement time, when the fact was said: the start of the
-- source episode. NULL means unknown, and readers fall back to learned_at,
-- which stays ingestion time.
-- Backfill, no LLM: dated rows become one-day intervals, and a row with a
-- source episode gets that episode's start as mentioned_at. A row with no
-- episode stays NULL: copying learned_at would add nothing readers do not
-- already fall back to, and would rewrite every such row under this
-- migration's exclusive lock. Every UPDATE touches only rows still NULL, so
-- a second run changes nothing.
-- The mentioned_at backfill rewrites heart.facts' HNSW indexes and takes about
-- a minute on a 25k-fact table (47 s measured on the 2026-11-03 prod copy).
-- The backfill is not an edit, so it must not move updated_at. The
-- set_updated_at trigger (init.sql) would stamp every row it fills, and the
-- heartbeat stale count and anything else that reads updated_at would then
-- see every fact as edited today. The trigger is off for the two UPDATEs
-- only. The application role owns heart.facts, which is all DISABLE TRIGGER
-- needs for a named trigger, and the migrator runs every migration in one
-- transaction, so a failure rolls the DISABLE back with the rest.
-- No index on valid_from: the date leg walks HNSW and applies its predicates
-- after, so a btree there would only be written, never read.
-- Plain statements, no DO block and no semicolon in a comment.
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS valid_from DATE;

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS valid_to DATE;

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS mentioned_at TIMESTAMPTZ;

ALTER TABLE heart.facts DISABLE TRIGGER set_updated_at;

UPDATE heart.facts
    SET valid_from = event_date, valid_to = event_date
    WHERE event_date IS NOT NULL AND valid_from IS NULL;

UPDATE heart.facts AS f
    SET mentioned_at = e.started_at
    FROM heart.episodes AS e
    WHERE f.mentioned_at IS NULL
      AND f.source_episode_id IS NOT NULL
      AND e.id = f.source_episode_id;

ALTER TABLE heart.facts ENABLE TRIGGER set_updated_at;
