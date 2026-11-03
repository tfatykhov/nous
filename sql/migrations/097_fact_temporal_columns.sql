-- 097: event time and statement time on facts (bi-temporal facts, PR1).
-- valid_from and valid_to are event time, the days the fact was true.
-- A one-day event has valid_from = valid_to = event_date. A closed span or a
-- coarse date ("Feb 2026") has both set and event_date NULL. An open state
-- ("since Jan") has valid_to NULL. No known date leaves all three NULL.
-- mentioned_at is statement time, when the fact was said: the start of the
-- source episode. NULL means unknown, and readers fall back to learned_at,
-- which stays ingestion time.
-- Backfill, no LLM: dated rows become one-day intervals, and mentioned_at
-- comes from the source episode's start, else learned_at. Every UPDATE
-- touches only rows still NULL, so a second run changes nothing.
-- No index on valid_from: the date leg walks HNSW and applies its predicates
-- after, so a btree there would only be written, never read.
-- Plain statements, no DO block and no semicolon in a comment.
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS valid_from DATE;

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS valid_to DATE;

ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS mentioned_at TIMESTAMPTZ;

UPDATE heart.facts
    SET valid_from = event_date, valid_to = event_date
    WHERE event_date IS NOT NULL AND valid_from IS NULL;

UPDATE heart.facts AS f
    SET mentioned_at = COALESCE(
        (SELECT e.started_at FROM heart.episodes AS e WHERE e.id = f.source_episode_id),
        f.learned_at
    )
    WHERE f.mentioned_at IS NULL;
